from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ContextPolicy:
    max_recent_actions: int | None = None
    max_facts: int = 24
    max_failures: int = 8
    max_artifacts: int = 100
    max_memory: int = 12
    max_observation_refs: int = 12
    fallback_order: tuple[str, ...] = ("memory", "facts", "actions", "artifacts", "failures")
