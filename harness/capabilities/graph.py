from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable


@dataclass(frozen=True)
class Capability:
    name: str
    description: str = ""
    required_permission: str = "read_only"


@dataclass(frozen=True)
class Implementation:
    name: str
    capability: str
    kind: str = "tool"
    executable: str | None = None
    required_permission: str = "read_only"
    cost: int = 1
    risk: int = 0
    deterministic: bool = True
    precondition: Callable[[Any], bool] | None = None


@dataclass(frozen=True)
class CapabilityResolution:
    capability: str
    selected: Implementation | None
    alternatives: tuple[Implementation, ...]
    rejected: tuple[dict[str, str], ...]
    strategy: str

    @property
    def available(self) -> bool:
        return self.selected is not None

    def dictionary(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "selected": self.selected.name if self.selected else None,
            "alternatives": [item.name for item in self.alternatives],
            "rejected": list(self.rejected), "strategy": self.strategy,
        }


class CapabilityGraph:
    def __init__(self):
        self.capabilities: dict[str, Capability] = {}
        self.implementations: dict[str, list[Implementation]] = {}

    def register_capability(self, capability: Capability) -> None:
        self.capabilities[capability.name] = capability

    def register(self, implementation: Implementation) -> None:
        self.implementations.setdefault(implementation.capability, []).append(implementation)

    def resolve_capability(self, capability: str, environment: Any,
                           permission: str = "workspace_write") -> CapabilityResolution:
        values = self.implementations.get(capability, [])
        rank = {"read_only": 0, "workspace_write": 1, "full_access": 2}
        available: list[Implementation] = []
        rejected: list[dict[str, str]] = []
        for item in values:
            if rank.get(item.required_permission, 2) > rank.get(permission, 0):
                rejected.append({"name": item.name, "reason": "permission"})
                continue
            if item.executable and not _has_executable(environment, item.executable):
                rejected.append({"name": item.name, "reason": f"missing executable: {item.executable}"})
                continue
            if item.precondition and not item.precondition(environment):
                rejected.append({"name": item.name, "reason": "precondition"})
                continue
            available.append(item)
        available.sort(key=lambda item: (0 if item.kind == "reuse" else 1 if item.kind == "reconfigure" else 2,
                                         item.cost, item.risk, not item.deterministic, item.name))
        selected = available[0] if available else None
        return CapabilityResolution(capability, selected, tuple(available), tuple(rejected),
                                    "reuse → reconfigure → install/mutate → LLM")


def default_capability_graph() -> CapabilityGraph:
    graph = CapabilityGraph()
    graph.register_capability(Capability("file_inspection", "Inspect source or binary files."))
    graph.register(Implementation("python_file_reader", "file_inspection", kind="reuse", executable="python3", cost=1))
    graph.register(Implementation("readelf", "file_inspection", kind="reconfigure", executable="readelf", cost=2))
    graph.register(Implementation("objdump", "file_inspection", kind="reconfigure", executable="objdump", cost=2))
    graph.register_capability(Capability(
        "binary_metadata", "Inspect binary metadata with an available executable."
    ))
    graph.register(Implementation(
        "file", "binary_metadata", kind="reuse", executable="file", cost=1
    ))
    graph.register(Implementation(
        "readelf", "binary_metadata", kind="reconfigure", executable="readelf", cost=2
    ))
    graph.register(Implementation(
        "objdump", "binary_metadata", kind="reconfigure", executable="objdump", cost=3
    ))
    return graph


def _has_executable(environment: Any, executable: str) -> bool:
    if isinstance(environment, dict):
        values = environment.get("executables", environment)
        return bool(values.get(executable)) if isinstance(values, dict) else False
    values = getattr(environment, "executables", {})
    return bool(values.get(executable))
