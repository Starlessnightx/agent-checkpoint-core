from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .client import LSPClient, LSPError, LSPTransport
from .transport import StdioLSPTransport


@dataclass(frozen=True)
class LSPServerConfig:
    language: str
    command: tuple[str, ...] = ()
    root: Path | None = None
    timeout_seconds: float = 10


class LSPManager:
    def __init__(self, executor: Any = None):
        self.clients: dict[str, LSPClient] = {}
        self.configs: dict[str, LSPServerConfig] = {}
        self.executor = executor

    def checkpoint_state(self) -> dict[str, Any]:
        if self.clients:
            raise RuntimeError("LSP sessions require an explicit checkpoint capability provider")
        return {"schema_version": 1, "clients": []}

    def configure(
        self, config: LSPServerConfig, transport: LSPTransport | None = None
    ) -> None:
        root = (config.root or Path.cwd()).resolve()
        if transport is None:
            if self.executor is None:
                raise LSPError("real LSP stdio requires an Executor", kind="transport_unavailable")
            transport = StdioLSPTransport(
                config.command, root, self.executor.spawn_stdio
            )
        self.configs[config.language] = config
        self.clients[config.language] = LSPClient(
            config.language, transport, root.as_uri(), config.timeout_seconds
        )

    def start(self, language: str) -> None:
        client = self.clients[language]
        try:
            client.start()
        except Exception:
            try:
                client.close()
            except Exception:
                pass
            raise

    def sync_document(self, language: str, path: Path, text: str, version: int = 1) -> None:
        self.clients[language].sync_document(path.resolve().as_uri(), text, version)

    def request(self, language: str, method: str, params: dict[str, Any]) -> Any:
        return self.clients[language].request(method, params)

    def diagnostics(self, language: str, uri: str) -> list[dict[str, Any]] | None:
        return self.clients[language].published_diagnostics(uri)

    def available(self, language: str) -> bool:
        return language in self.clients and self.clients[language].initialized

    def shutdown(self, language: str) -> None:
        self.clients[language].close()

    def shutdown_all(self) -> None:
        for language in tuple(self.clients):
            try:
                self.shutdown(language)
            except Exception:
                continue

    def process(self, language: str) -> Any:
        return getattr(self.clients[language].transport, "process", None)
