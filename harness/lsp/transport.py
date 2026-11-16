from __future__ import annotations

import json
import os
import selectors
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .client import LSPError


class StdioLSPTransport:
    """JSON-RPC transport using the LSP Content-Length framing protocol."""

    def __init__(
        self,
        command: tuple[str, ...],
        cwd: Path,
        process_factory: Callable[
            [tuple[str, ...], Path], subprocess.Popen[bytes]
        ],
    ):
        self.command = tuple(command)
        self.cwd = cwd.resolve()
        self.process_factory = process_factory
        self.process: subprocess.Popen[bytes] | None = None
        self.started = False
        self.notifications: list[tuple[str, dict[str, Any]]] = []
        self.sent_methods: list[str] = []
        self._buffer = bytearray()
        self._request_id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.started:
            return
        if not self.command:
            raise LSPError("language server command is not configured", kind="transport_unavailable")
        try:
            self.process = self.process_factory(self.command, self.cwd)
        except OSError as exc:
            raise LSPError(f"failed to start language server: {exc}", kind="spawn_failed") from exc
        self.started = True

    def request(self, method: str, params: dict[str, Any], timeout: float) -> Any:
        if not self.started:
            raise LSPError("LSP transport is not started", kind="not_started")
        with self._lock:
            self._request_id += 1
            identifier = self._request_id
            self._write({
                "jsonrpc": "2.0", "id": identifier,
                "method": method, "params": params,
            })
            deadline = time.monotonic() + timeout
            while True:
                message = self._read_message(max(0.0, deadline - time.monotonic()))
                if "method" in message:
                    self._handle_incoming(message)
                    continue
                if message.get("id") != identifier:
                    continue
                if "error" in message:
                    error = message.get("error") or {}
                    raise LSPError(
                        str(error.get("message", "LSP JSON-RPC error")),
                        kind="method_not_supported" if error.get("code") == -32601 else "protocol_error",
                    )
                return message.get("result")

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.started:
            raise LSPError("LSP transport is not started", kind="not_started")
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def wait_notification(
        self, method: str, predicate: Callable[[dict[str, Any]], bool], timeout: float
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + timeout
        with self._lock:
            while True:
                for index, (candidate, params) in enumerate(self.notifications):
                    if candidate == method and predicate(params):
                        self.notifications.pop(index)
                        return params
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                try:
                    message = self._read_message(remaining)
                except LSPError as exc:
                    if exc.kind == "timeout":
                        return None
                    raise
                self._handle_incoming(message)

    def close(self) -> None:
        process, self.process = self.process, None
        self.started = False
        self._buffer.clear()
        if process is None:
            return
        if process.stdin is not None:
            try:
                process.stdin.close()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
        for stream in (process.stdout, process.stderr):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _handle_incoming(self, message: dict[str, Any]) -> None:
        method = str(message.get("method", ""))
        params = message.get("params") or {}
        if "id" not in message:
            self.notifications.append((method, params if isinstance(params, dict) else {}))
            return
        if method == "workspace/configuration":
            items = params.get("items", []) if isinstance(params, dict) else []
            result: Any = [{} for _ in items]
        elif method == "workspace/workspaceFolders":
            result = None
        else:
            result = None
        self._write({"jsonrpc": "2.0", "id": message["id"], "result": result})

    def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise LSPError(self._process_error(), kind="transport_closed")
        payload = json.dumps(message, separators=(",", ":")).encode("utf-8")
        if "method" in message:
            self.sent_methods.append(str(message["method"]))
        framed = f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii") + payload
        try:
            process.stdin.write(framed)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise LSPError(self._process_error(), kind="transport_closed") from exc

    def _read_message(self, timeout: float) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        marker = b"\r\n\r\n"
        while marker not in self._buffer:
            self._read_chunk(deadline)
        header, remainder = bytes(self._buffer).split(marker, 1)
        length = None
        for line in header.decode("ascii", errors="replace").split("\r\n"):
            name, _, value = line.partition(":")
            if name.casefold() == "content-length":
                try:
                    length = int(value.strip())
                except ValueError as exc:
                    raise LSPError("invalid LSP Content-Length", kind="protocol_error") from exc
        if length is None or length < 0 or length > 16 * 1024 * 1024:
            raise LSPError("invalid or excessive LSP message length", kind="protocol_error")
        self._buffer = bytearray(remainder)
        while len(self._buffer) < length:
            self._read_chunk(deadline)
        payload = bytes(self._buffer[:length])
        del self._buffer[:length]
        try:
            value = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LSPError("invalid LSP JSON-RPC response", kind="protocol_error") from exc
        if not isinstance(value, dict):
            raise LSPError("LSP response must be an object", kind="protocol_error")
        return value

    def _read_chunk(self, deadline: float) -> None:
        process = self.process
        if process is None or process.stdout is None:
            raise LSPError("language server process is unavailable", kind="transport_closed")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise LSPError("LSP request timed out", kind="timeout", retryable=True)
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(remaining):
                raise LSPError("LSP request timed out", kind="timeout", retryable=True)
            chunk = os.read(process.stdout.fileno(), 65536)
        finally:
            selector.close()
        if not chunk:
            raise LSPError(self._process_error(), kind="transport_closed")
        self._buffer.extend(chunk)

    def _process_error(self) -> str:
        process = self.process
        if process is None:
            return "language server process is unavailable"
        detail = ""
        if process.poll() is not None and process.stderr is not None:
            try:
                detail = process.stderr.read().decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
        return "language server process exited" + (f": {detail[:2000]}" if detail else "")


__all__ = ["StdioLSPTransport"]
