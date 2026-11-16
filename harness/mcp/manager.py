from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .adapter import MCPToolAdapter
from .client import MCPClient, MCPTransport, StdioTransport


@dataclass(frozen=True)
class MCPServerConfig:
    name: str
    command: tuple[str, ...] = ()
    cwd: Path | None = None
    timeout_seconds: float = 30


@dataclass
class _Managed:
    config: MCPServerConfig
    client: MCPClient
    adapter: MCPToolAdapter
    catalog: tuple[dict[str, Any], ...] = ()
    registered: set[str] = field(default_factory=set)


class MCPManager:
    def __init__(self, executor: Any = None):
        self._servers: dict[str, _Managed] = {}
        self.executor = executor

    def checkpoint_state(self) -> dict[str, Any]:
        if self._servers:
            raise RuntimeError("MCP sessions require an explicit checkpoint capability provider")
        return {"schema_version": 1, "servers": []}

    def configure(self, config: MCPServerConfig, transport: MCPTransport | None = None) -> None:
        client = MCPClient(
            config.name,
            transport or StdioTransport(
                command=config.command,
                cwd=config.cwd,
                process_factory=(
                    self.executor.spawn_stdio if self.executor is not None else None
                ),
            ),
            config.timeout_seconds,
        )
        self._servers[config.name] = _Managed(config, client, MCPToolAdapter(client))

    def start(self, name: str) -> tuple[dict[str, Any], ...]:
        managed = self._servers[name]
        try:
            managed.client.start()
            managed.catalog = managed.client.list_tools()
        except Exception:
            try:
                managed.client.shutdown()
            except Exception:
                pass
            raise
        return managed.catalog

    def catalog(self, name: str) -> tuple[dict[str, Any], ...]:
        return self._servers[name].catalog

    def register_tools(
        self,
        name: str,
        registry: Any,
        raw_names: tuple[str, ...] = (),
        *,
        activate: bool = True,
    ) -> tuple[str, ...]:
        managed = self._servers[name]
        requested = set(raw_names) if raw_names else {str(item.get("name", "")) for item in managed.catalog}
        added = []
        for raw in managed.catalog:
            raw_name = str(raw.get("name", ""))
            if raw_name not in requested or raw_name in managed.registered:
                continue
            spec = managed.adapter.spec(raw)
            if spec.name in registry.specs:
                continue
            registry.register(spec)
            managed.registered.add(raw_name)
            if activate:
                registry.router.activate((spec.name,))
            added.append(spec.name)
        return tuple(added)

    def publish_catalog(self, name: str, registry: Any) -> tuple[str, ...]:
        """Register searchable schemas without exposing them to the model."""
        return self.register_tools(name, registry, activate=False)

    def shutdown(self, name: str) -> None:
        self._servers[name].client.shutdown()

    def shutdown_all(self) -> None:
        for name in tuple(self._servers):
            try:
                self.shutdown(name)
            except Exception:
                # Every client closes its transport in a finally block. Continue
                # releasing the remaining servers during Session.close().
                continue

    def process(self, name: str) -> Any:
        """Read-only lifecycle inspection used by integration tests and diagnostics."""
        return getattr(self._servers[name].client.transport, "process", None)
