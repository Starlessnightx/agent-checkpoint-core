from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class LSPError(RuntimeError):
    def __init__(self, message: str, *, kind: str = "lsp_error", retryable: bool = False):
        super().__init__(message)
        self.kind, self.retryable = kind, retryable


class LSPTransport(Protocol):
    def start(self) -> None: ...
    def request(self, method: str, params: dict[str, Any], timeout: float) -> Any: ...
    def notify(self, method: str, params: dict[str, Any]) -> None: ...
    def close(self) -> None: ...


@dataclass
class LSPClient:
    language: str
    transport: LSPTransport
    root_uri: str
    timeout_seconds: float = 10
    initialized: bool = False
    documents: dict[str, tuple[int, str]] | None = None

    def start(self) -> None:
        self.transport.start()
        self.transport.request("initialize", {
            "processId": None,
            "rootUri": self.root_uri,
            "capabilities": {
                "textDocument": {"publishDiagnostics": {}},
                "workspace": {"configuration": True},
            },
            "workspaceFolders": [{"uri": self.root_uri, "name": self.language}],
        }, self.timeout_seconds)
        self.transport.notify("initialized", {})
        self.initialized = True
        self.documents = {}

    def sync_document(self, uri: str, text: str, version: int = 1) -> None:
        if not self.initialized:
            raise LSPError("LSP client is not initialized", kind="not_initialized")
        documents = self.documents if self.documents is not None else {}
        previous = documents.get(uri)
        if previous is None:
            current_version = max(1, version)
            self.transport.notify("textDocument/didOpen", {
                "textDocument": {"uri": uri, "languageId": self.language,
                                  "version": current_version, "text": text},
            })
        elif previous[1] != text:
            current_version = max(previous[0] + 1, version)
            self.transport.notify("textDocument/didChange", {
                "textDocument": {"uri": uri, "version": current_version},
                "contentChanges": [{"text": text}],
            })
        else:
            return
        documents[uri] = (current_version, text)
        self.documents = documents

    def request(self, method: str, params: dict[str, Any]) -> Any:
        if not self.initialized:
            raise LSPError("LSP client is not initialized", kind="not_initialized")
        return self.transport.request(method, params, self.timeout_seconds)

    def published_diagnostics(self, uri: str) -> list[dict[str, Any]] | None:
        waiter = getattr(self.transport, "wait_notification", None)
        if not callable(waiter):
            return None
        params = waiter(
            "textDocument/publishDiagnostics",
            lambda value: str(value.get("uri", "")) == uri,
            self.timeout_seconds,
        )
        if not params:
            return None
        values = params.get("diagnostics") or []
        return values if isinstance(values, list) else []

    def close(self) -> None:
        try:
            if self.initialized:
                self.transport.request("shutdown", {}, self.timeout_seconds)
                self.transport.notify("exit", {})
        finally:
            self.transport.close()
            self.initialized = False
            self.documents = None
