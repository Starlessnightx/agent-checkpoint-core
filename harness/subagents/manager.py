from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .coordinator import SubagentCoordinator


@dataclass(frozen=True)
class AgentSpec:
    name: str
    model: str = "default"
    system_prompt: str = ""
    capabilities: tuple[str, ...] = ()
    permission: str = "read_only"
    max_turns: int | None = None
    token_budget: int | None = None
    workspace_policy: str = "read_only"
    skills: tuple[str, ...] = ()
    max_tool_calls: int | None = None
    context_policy: Any = None
    max_context_tokens: int | None = None


class AgentManager:
    """Stable lifecycle facade over the bounded SubagentCoordinator."""

    def __init__(self, coordinator: SubagentCoordinator):
        self.coordinator = coordinator

    def spawn(self, agent: str | AgentSpec, task: str, *, context_refs: list[str] | None = None,
              workspace_mode: str | None = None, foreground: bool = False) -> str:
        role = agent.name if isinstance(agent, AgentSpec) else agent
        mode = workspace_mode or (agent.workspace_policy if isinstance(agent, AgentSpec) else None)
        agent_id = self.coordinator.spawn(
            role, task, context_refs or [], mode,
            agent_spec=agent if isinstance(agent, AgentSpec) else None,
        )
        if foreground:
            self.collect(agent_id, timeout_seconds=-1)
        return agent_id

    def status(self, agent_id: str) -> dict[str, Any]:
        return self.coordinator.status(agent_id)

    def steer(self, agent_id: str, instruction: str) -> bool:
        return self.coordinator.steer(agent_id, instruction)

    def collect(self, agent_id: str, timeout_seconds: float = 0) -> dict[str, Any] | None:
        result = self.coordinator.collect(agent_id, timeout_seconds)
        return result.bounded_dictionary() if result is not None else None

    def cancel(self, agent_id: str) -> bool:
        return self.coordinator.cancel(agent_id)

    def spawn_many(self, requests: list[dict[str, Any]]) -> tuple[str, ...]:
        return tuple(self.spawn(
            request["agent"], request["task"],
            context_refs=request.get("context_refs"),
            workspace_mode=request.get("workspace_mode"),
        ) for request in requests)

    def close(self, wait: bool = True) -> None:
        self.coordinator.close(wait=wait)
