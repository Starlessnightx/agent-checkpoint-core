from __future__ import annotations

from .config import HarnessConfig
from .core.runtime import AgentRuntime
from .spec import HarnessSpec
from .types import AgentResult, ChatModel
from .permissions import PermissionPolicy, Sandbox
from .executor import Executor
from .core.boundaries import BoundaryFactory


class CodingAgent:
    """Backward-compatible facade for the session-based runtime kernel."""

    def __init__(self, config: HarnessConfig | HarnessSpec, model: ChatModel, *,
                 permission_policy: PermissionPolicy | None = None,
                 sandbox: Sandbox | None = None, executor: Executor | None = None,
                 boundary_factory: BoundaryFactory | None = None):
        self.spec = getattr(config, "spec", config)
        self.model = model
        self.runtime = AgentRuntime(self.spec, model, permission_policy=permission_policy,
                                    sandbox=sandbox, executor=executor, boundary_factory=boundary_factory)

    def run(self, task: str, *, engine: str = "turn") -> AgentResult:
        """Synchronous caller adapter; async callers must await run_async."""
        return self.runtime.run(task, engine=engine)

    async def run_async(self, task: str) -> AgentResult:
        """Execute through the session-owned turn loop."""
        from .tasks.oneshot import run_once

        return await run_once(self.runtime, task)


__all__ = ["CodingAgent"]
