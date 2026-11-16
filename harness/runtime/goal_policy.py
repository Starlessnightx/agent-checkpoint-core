from __future__ import annotations

from typing import Any


class GoalPolicy:
    """Turn sampling budget and delegated-task guidance."""

    def __init__(self, policy: Any):
        self.policy = policy

    def checkpoint_state(self) -> dict[str, Any]:
        return {"schema_version": 1, "policy": self.policy}

    @property
    def phase(self) -> str:
        return "active"

    @property
    def max_turns(self) -> int | None:
        value = self.policy.get("max_turns") if isinstance(self.policy, dict) else self.policy.max_turns
        return None if value is None else int(value)

    def initial_instruction(self) -> str:
        if isinstance(self.policy, dict) and self.policy.get("allow_planning") is False:
            return (
                "Execute the delegated scope using its inputs, hypothesis and acceptance criteria. "
                "Choose actions from evidence without mandatory stages or a force-write quota. "
                "Return artifacts, verification and unresolved limitations to the parent."
            )
        return (
            "Work toward the exact objective and success conditions. For complex work, "
            "use update_plan to persist dependencies, hypotheses, acceptance evidence, "
            "conditional next decisions and delegation contracts. Test uncertain methods "
            "with representative experiments before expensive execution. Choose the next "
            "useful action from evidence and ready work; no fixed work stages or force-write quota."
        )
