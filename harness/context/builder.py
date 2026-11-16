from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .policy import ContextPolicy
from .store import ContextStore
from harness.planning.store import project_plan


@dataclass(frozen=True)
class ModelContext:
    """The bounded projection passed to a model for one runtime turn."""

    messages: list[dict[str, Any]]
    estimated_tokens: int
    observation_refs: list[str]


class ContextBuilder:
    """Build a compact, structured model view without losing source events."""

    def __init__(self, policy: ContextPolicy | None = None):
        self.policy = policy or ContextPolicy()

    def build(self, store: ContextStore) -> dict[str, Any]:
        policy = self.policy
        recent_actions = (
            store.actions
            if policy.max_recent_actions is None
            else (
                []
                if policy.max_recent_actions == 0
                else store.actions[-policy.max_recent_actions:]
            )
        )
        return {
            "task_contract": store.task_contract,
            "current_goal": store.goal or store.task_contract,
            "plan": store.plan[-policy.max_facts:],
            "planning": project_plan(store.planning) if store.planning else {},
            "recent_actions": recent_actions,
            "facts": store.facts[-policy.max_facts:],
            "artifacts": sorted(store.artifacts)[-policy.max_artifacts:],
            "latest_failure": store.failures[-1] if store.failures else None,
            "recent_failures": store.failures[-policy.max_failures:],
            "verification_state": dict(store.verification),
            "environment_summary": dict(store.environment),
            "active_skills": list(store.active_skills),
            "memory": store.memory[-policy.max_memory:],
            "observation_refs": store.observation_refs[-policy.max_observation_refs:],
        }

    def prompt(self, store: ContextStore) -> str:
        import json
        return "Structured model view (source events remain auditable):\n" + json.dumps(
            self.build(store), ensure_ascii=False, sort_keys=True, indent=2
        )
