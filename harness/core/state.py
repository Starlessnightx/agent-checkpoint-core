from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any


@dataclass
class RuntimeState:
    phase: str = "start"
    turn: int = 0
    status: str = "created"
    termination: str | None = None
    rejected_completions: int = 0
    final_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class UsageState:
    input_tokens: int = 0
    cache_read_tokens: int = 0
    cache_miss_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def update(self, value: dict[str, Any]) -> None:
        for name in (
            "input_tokens", "cache_read_tokens", "cache_miss_tokens",
            "output_tokens", "requests",
        ):
            if name in value:
                setattr(self, name, int(value.get(name) or 0))

    def add(self, value: dict[str, Any]) -> None:
        for name in (
            "input_tokens", "cache_read_tokens", "cache_miss_tokens",
            "output_tokens", "requests",
        ):
            if name in value:
                setattr(self, name, getattr(self, name) + int(value.get(name) or 0))

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def dictionary(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_miss_tokens": self.cache_miss_tokens,
            "output_tokens": self.output_tokens,
            "requests": self.requests,
        }


@dataclass
class TaskUsageLedger:
    """One task-wide counter shared by parent and concurrent child sessions."""

    usage: UsageState = field(default_factory=UsageState)
    cost_usd: float = 0.0
    lock: Lock = field(default_factory=Lock, repr=False)

    def add(self, value: dict[str, Any], *, cost_usd: float = 0.0) -> dict[str, int | float]:
        with self.lock:
            self.usage.add(value)
            self.cost_usd += cost_usd
            return {**self.usage.dictionary(), "cost_usd": self.cost_usd}

    def snapshot(self) -> dict[str, int | float]:
        with self.lock:
            return {**self.usage.dictionary(), "cost_usd": self.cost_usd}


@dataclass
class ArtifactState:
    paths: set[str] = field(default_factory=set)

    def apply(self, delta: dict[str, Any] | None) -> tuple[str, ...]:
        if not delta:
            return ()
        changed = {
            str(path)
            for key in ("created", "modified", "deleted")
            for path in (delta.get(key) or [])
        }
        self.paths.update(str(path) for path in delta.get("created") or [])
        self.paths.update(str(path) for path in delta.get("modified") or [])
        self.paths.difference_update(str(path) for path in delta.get("deleted") or [])
        return tuple(sorted(changed))


__all__ = ["ArtifactState", "RuntimeState", "TaskUsageLedger", "UsageState"]
