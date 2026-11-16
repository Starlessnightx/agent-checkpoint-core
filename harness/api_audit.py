"""Scoped provider-boundary audit and compressed, content-addressed JSON storage."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
import gzip
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Iterator, Protocol

from harness.executor.base import Executor


class AuditSink(Protocol):
    def __call__(self, stage: str, body: Any, metadata: dict[str, Any]) -> None: ...


_sink: ContextVar[AuditSink | None] = ContextVar("provider_audit_sink", default=None)
_SENSITIVE = {"authorization", "api_key", "apikey", "access_token", "refresh_token"}


class ApiAuditError(RuntimeError):
    """Audit failure must never be mistaken for a retryable provider error."""


@contextmanager
def bind_api_audit(sink: AuditSink) -> Iterator[None]:
    token = _sink.set(sink)
    try:
        yield
    finally:
        _sink.reset(token)


def record_wire(stage: str, body: Any, *, secret: str, **metadata: Any) -> None:
    sink = _sink.get()
    if sink is None:
        return

    def redact(value):
        if isinstance(value, dict):
            return {key: "[REDACTED]" if key.lower() in _SENSITIVE else redact(item)
                    for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item) for item in value]
        if isinstance(value, str) and secret:
            return value.replace(secret, "[REDACTED]")
        return value

    # Headers are deliberately never passed to this interface. Preserve all
    # non-credential content, including tool schemas and provider usage fields.
    try:
        sink(stage, redact(body), redact(metadata))
    except Exception as exc:
        raise ApiAuditError(f"provider audit persistence failed ({type(exc).__name__})") from exc


def _json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


class ApiAuditStore:
    def __init__(self, directory: Path, executor: Executor, emit: Callable[..., Any]):
        self.directory, self.executor, self.emit = directory, executor, emit

    def _put(self, value: Any) -> str:
        raw = _json(value)
        digest = hashlib.sha256(raw).hexdigest()
        path = self.directory / "objects" / f"{digest}.json.gz"
        if not self.executor.exists(path):
            self.executor.write_file(path, gzip.compress(raw, mtime=0))
        return digest

    def __call__(self, stage: str, body: Any, metadata: dict[str, Any]) -> None:
        # Split growing message/schema lists so unchanged history is stored once.
        if isinstance(body, dict):
            parts = {key: {"items": [self._put(item) for item in value]}
                     if isinstance(value, list) else {"value": self._put(value)}
                     for key, value in body.items()}
            manifest = {"version": 1, "kind": "object", "parts": parts}
        else:
            manifest = {"version": 1, "kind": "value", "value": self._put(body)}
        reference = self._put(manifest)
        self.emit(f"api_{stage}", **metadata, payload_ref=f"api/objects/{reference}.json.gz",
                  payload_sha256=hashlib.sha256(_json(body)).hexdigest(),
                  audit_format="json-parts-v1", credential_redaction=True)


def load_payload(directory: Path, reference: str) -> Any:
    """Reconstruct a captured, credential-redacted body, verifying every object."""
    def read(digest):
        if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("invalid audit object digest")
        raw = gzip.decompress((directory / "objects" / f"{digest}.json.gz").read_bytes())
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("audit object hash mismatch")
        return json.loads(raw)

    name = Path(reference).name
    if not name.endswith(".json.gz"):
        raise ValueError("invalid audit payload reference")
    manifest = read(name[:-8])
    if manifest.get("version") != 1:
        raise ValueError("unsupported audit format")
    if manifest["kind"] == "value":
        return read(manifest["value"])
    return {key: [read(digest) for digest in part["items"]] if "items" in part
            else read(part["value"]) for key, part in manifest["parts"].items()}
