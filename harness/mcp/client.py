from __future__ import annotations

import json
import selectors
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


class MCPError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "mcp_error", retryable: bool = False):
        super().__init__(message)
        self.kind, self.retryable = kind, retryable


class MCPTransport(Protocol):
    def start(self) -> None: ...
    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]: ...
    def notify(self, method: str, params: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


class StdioTransport:
    """Synchronous JSON-RPC transport over an MCP server's real stdio pipes.

    ``exchange`` remains as a compatibility seam for unit tests. Production
    configurations provide ``command`` and create a managed child process.
    """

    def __init__(
        self,
        exchange: Callable[[str, dict[str, Any], float], dict[str, Any]] | None = None,
        *,
        command: tuple[str, ...] = (),
        cwd: Path | None = None,
        process_factory: Callable[
            [tuple[str, ...], Path], subprocess.Popen[bytes]
        ] | None = None,
    ):
        self.exchange = exchange
        self.command = tuple(command)
        self.cwd = cwd.resolve() if cwd else None
        self.process_factory = process_factory
        self.started = False
        self.process: subprocess.Popen[bytes] | None = None
        self._request_id = 0
        self._lock = threading.Lock()

    def start(self) -> None:
        if self.started:
            return
        if self.exchange is None:
            if not self.command:
                raise MCPError(
                    "stdio server command is not configured",
                    kind="transport_unavailable",
                )
            try:
                if self.process_factory is not None:
                    if self.cwd is None:
                        raise MCPError(
                            "executor-backed MCP transport requires a working directory",
                            kind="transport_unavailable",
                        )
                    self.process = self.process_factory(self.command, self.cwd)
                else:
                    raise MCPError(
                        "real MCP stdio requires an Executor process factory",
                        kind="transport_unavailable",
                    )
            except MCPError:
                raise
            except OSError as exc:
                raise MCPError(
                    f"failed to start MCP stdio server: {exc}", kind="spawn_failed"
                ) from exc
        self.started = True

    def request(self, method: str, params: dict[str, Any], timeout: float) -> dict[str, Any]:
        if not self.started:
            raise MCPError("stdio transport is not started", kind="not_started")
        if self.exchange is not None:
            try:
                return self.exchange(method, params, timeout)
            except TimeoutError as exc:
                raise MCPError(
                    str(exc) or "MCP request timed out",
                    kind="timeout",
                    retryable=True,
                ) from exc
        with self._lock:
            self._request_id += 1
            identifier = self._request_id
            self._write({
                "jsonrpc": "2.0", "id": identifier,
                "method": method, "params": params,
            })
            deadline = time.monotonic() + timeout
            while True:
                message = self._read(max(0.0, deadline - time.monotonic()))
                if message.get("id") != identifier:
                    # Server notifications are valid between request and response.
                    continue
                if "error" in message:
                    error = message.get("error") or {}
                    raise MCPError(
                        str(error.get("message", "MCP JSON-RPC error")),
                        kind="protocol_error",
                    )
                result = message.get("result", {})
                if not isinstance(result, dict):
                    raise MCPError("MCP result must be an object", kind="protocol_error")
                return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.started:
            raise MCPError("stdio transport is not started", kind="not_started")
        # Compatibility exchanges model request/response only.
        if self.exchange is not None:
            return
        with self._lock:
            self._write({"jsonrpc": "2.0", "method": method, "params": params})

    def close(self) -> None:
        process, self.process = self.process, None
        self.started = False
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

    def _write(self, message: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None or process.poll() is not None:
            raise MCPError(self._process_error(), kind="transport_closed")
        try:
            process.stdin.write(
                json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
            )
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise MCPError(self._process_error(), kind="transport_closed") from exc

    def _read(self, timeout: float) -> dict[str, Any]:
        process = self.process
        if process is None or process.stdout is None:
            raise MCPError("MCP stdio process is unavailable", kind="transport_closed")
        selector = selectors.DefaultSelector()
        try:
            selector.register(process.stdout, selectors.EVENT_READ)
            if not selector.select(timeout):
                raise MCPError(
                    "MCP request timed out", kind="timeout", retryable=True
                )
            line = process.stdout.readline()
        finally:
            selector.close()
        if not line:
            raise MCPError(self._process_error(), kind="transport_closed")
        try:
            value = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise MCPError("invalid MCP JSON-RPC response", kind="protocol_error") from exc
        if not isinstance(value, dict):
            raise MCPError("MCP response must be an object", kind="protocol_error")
        return value

    def _process_error(self) -> str:
        process = self.process
        if process is None:
            return "MCP stdio process is unavailable"
        detail = ""
        if process.poll() is not None and process.stderr is not None:
            try:
                detail = process.stderr.read().decode("utf-8", errors="replace").strip()
            except OSError:
                detail = ""
        return "MCP stdio process exited" + (f": {detail[:2000]}" if detail else "")


@dataclass
class MCPClient:
    server_name: str
    transport: MCPTransport
    timeout_seconds: float = 30
    initialized: bool = False

    def start(self) -> dict[str, Any]:
        self.transport.start()
        result = self.transport.request("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "self-improving-coding-harness", "version": "4"},
        }, self.timeout_seconds)
        notifier = getattr(self.transport, "notify", None)
        if callable(notifier):
            notifier("notifications/initialized", {})
        self.initialized = True
        return result

    def list_tools(self) -> tuple[dict[str, Any], ...]:
        if not self.initialized:
            raise MCPError("MCP client is not initialized", kind="not_initialized")
        result = self.transport.request("tools/list", {}, self.timeout_seconds)
        return tuple(result.get("tools") or ())

    def call_tool(self, raw_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        if not self.initialized:
            raise MCPError("MCP client is not initialized", kind="not_initialized")
        return self.transport.request("tools/call", {"name": raw_name, "arguments": arguments}, self.timeout_seconds)

    def shutdown(self) -> None:
        try:
            if self.initialized:
                self.transport.request("shutdown", {}, self.timeout_seconds)
        finally:
            self.transport.close()
            self.initialized = False
