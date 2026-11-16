from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass(frozen=True)
class EnvironmentState:
    os: str
    arch: str
    cwd: str
    runtime: dict[str, Any] = field(default_factory=dict)
    executables: dict[str, str | None] = field(default_factory=dict)
    package_managers: dict[str, str | None] = field(default_factory=dict)
    environment: dict[str, Any] = field(default_factory=dict)
    permission: dict[str, Any] = field(default_factory=dict)
    services: dict[str, Any] = field(default_factory=dict)
    workspace: dict[str, Any] = field(default_factory=dict)
    network: dict[str, Any] = field(default_factory=dict)
    resource: dict[str, Any] = field(default_factory=dict)
    probes: tuple[dict[str, Any], ...] = ()

    def dictionary(self) -> dict[str, Any]:
        return asdict(self)
