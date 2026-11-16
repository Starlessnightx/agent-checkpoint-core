from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable
import re

from .base import ToolRequest, ToolSpec


@dataclass(frozen=True)
class RouteDecision:
    selected: tuple[str, ...]
    reason: str

    def dictionary(self) -> dict[str, Any]:
        return {"selected": list(self.selected), "reason": self.reason}


class ToolRouter:
    """Deterministic capability router with a conservative discovery fallback."""

    def __init__(self, registry: Any, graph: Any = None):
        self.registry = registry
        self.graph = graph
        self.last_resolution = None
        self.last_decision = RouteDecision((), "not routed")

    def select(self, task: str, state: Any = None, environment: Any = None) -> list[ToolSpec]:
        query = str(task or "").casefold()
        specs = list(self.registry.specs.values())
        bootstrap = [spec for spec in specs if spec.capability.startswith("skill:")]
        if self.registry.independent_tools:
            from .catalog import CORE_TOOLS

            bootstrap += [spec for spec in specs if spec.name in CORE_TOOLS and spec not in bootstrap]
        scored: list[tuple[int, str, ToolSpec]] = []
        for spec in specs:
            if spec in bootstrap:
                continue
            text = f"{spec.name} {spec.description} {spec.capability}".casefold()
            score = sum(1 for token in _tokens(query) if token in text)
            if score:
                scored.append((score, spec.name, spec))
        selected = bootstrap + [item[2] for item in sorted(scored, key=lambda item: (-item[0], item[1]))]
        if self.registry.independent_tools:
            selected = bootstrap  # Optional schemas enter only through explicit discovery.
        if not selected:
            selected = bootstrap or specs[:]
        allowed = []
        for spec in selected:
            permission = self.registry.permission_policy.evaluate(
                spec, ToolRequest(spec.name, {}), self.registry.context
            )
            if permission.decision.value != "deny":
                allowed.append(spec)
        self.last_decision = RouteDecision(tuple(spec.name for spec in allowed),
                                           "keyword capability match with bootstrap fallback")
        return allowed

    def search(self, query: str) -> tuple[ToolSpec, ...]:
        terms = set(_tokens(query))
        if not terms:
            return ()
        matches = []
        for spec in self.registry.specs.values():
            name = set(_tokens(spec.name))
            capability = set(_tokens(spec.capability))
            description = set(_tokens(spec.description))
            normalized = query.strip().casefold()
            exact = normalized.replace(" ", "_") == spec.name.casefold()
            cap = normalized == spec.capability.casefold() or spec.capability.casefold().startswith(normalized + ":")
            alias = _alias_score(terms, spec)
            hits = len(terms & (name | capability | description))
            if exact or cap or hits or alias:
                score = (4 if exact else 3 if cap else 2 if hits else 1,
                         len(terms & name), len(terms & capability), hits, alias)
                matches.append((score, spec.name, spec))
        return tuple(item[2] for item in sorted(matches, key=lambda item: (
            tuple(-part for part in item[0]), item[1])))

    def resolve_capability(self, capability: str, environment: Any = None,
                           permission: str = "workspace_write") -> Any:
        if self.graph is None:
            return None
        return self.graph.resolve_capability(capability, environment, permission)

    def activate(self, names: Iterable[str]) -> tuple[str, ...]:
        active = set(self.registry.active_tool_names)
        activated = []
        for name in names:
            if name not in self.registry.specs:
                continue
            spec = self.registry.specs[name]
            if self.registry.permission_policy.evaluate(
                spec, ToolRequest(name, {}), self.registry.context
            ).decision.value == "deny":
                continue
            active.add(name)
            activated.append(name)
        self.registry.active_tool_names = tuple(sorted(active))
        return tuple(activated)


def _tokens(value: str) -> tuple[str, ...]:
    stop = {"the", "and", "for", "with", "please", "tool", "tools", "to"}
    return tuple(token for token in re.findall(r"[^\W_]+", value.casefold())
                 if len(token) >= 2 and token not in stop)


def _alias_score(terms: set[str], spec: ToolSpec) -> int:
    groups = (({"shell", "terminal", "bash", "command", "终端", "命令"}, "process:", "run_command"),
              ({"file", "files", "folder", "directory", "edit", "文件", "目录"}, "filesystem:", "read_file"),
              ({"test", "tests", "check", "testing", "测试"}, "verification:", "run_check"))
    return max((2 if spec.name == preferred else 1 for aliases, prefix, preferred in groups
                if terms & aliases and (spec.capability.startswith(prefix) or spec.name == preferred)), default=0)
