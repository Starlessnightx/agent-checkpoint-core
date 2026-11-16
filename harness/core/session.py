from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from .submission import SubmissionLoop, TaskRunner
    from .boundaries import BoundaryHandler

from .events import EventRecorder
from .state import ArtifactState, RuntimeState, TaskUsageLedger, UsageState
from .task import TaskContract
from harness.protocol import Steer


@dataclass
class Session:
    session_id: str
    task: TaskContract
    messages: list[dict[str, Any]]
    state: RuntimeState
    usage: UsageState
    artifacts: ArtifactState
    recorder: EventRecorder
    orchestrator: Any
    recovery: Any
    tools: Any
    context_manager: Any
    subagents: Any
    hooks: Any = None
    agent_manager: Any = None
    environment: Any = None
    environment_repair: Any = None
    mcp_manager: Any = None
    lsp_manager: Any = None
    model: Any = None
    budget: Any = None
    token_budget: int | None = None
    task_usage: TaskUsageLedger | None = None
    tool_call_budget: int | None = None
    agent_role: str = "parent"
    parent_session_id: str | None = None
    model_context: Any = None
    closed: bool = False
    submission_loop: SubmissionLoop | None = None
    active_turn_id: str | None = None
    deferred_inputs: list[Steer] = field(default_factory=list)
    sampling_index: int = 0
    assembly_profile: str = "turn"
    workspace_lease: Any = None
    defer_lease_release: bool = False
    boundary_handler: BoundaryHandler | None = None
    api_audit_prefixes: tuple[Path, ...] = ()

    def release_lease(self) -> None:
        if self.workspace_lease is not None:
            self.workspace_lease.close()

    def discard_deferred_inputs(self, *, reason: str) -> tuple[str, ...]:
        """Explicit idle-only alternative to consuming guidance on the next user turn."""
        if self.closed or self.active_turn_id is not None:
            raise RuntimeError("deferred input can only be discarded in an open idle session")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("discarding deferred input requires a reason")
        pending = tuple(self.deferred_inputs)
        if not pending:
            return ()
        identifiers = tuple(item.operation_id for item in pending)
        self.emit("deferred_inputs_discarded", operation_ids=list(identifiers), reason=reason,
                  source_turn_ids=[item.turn_id for item in pending])
        self.deferred_inputs.clear()
        return identifiers

    def open_submission_loop(
        self, runner: TaskRunner, *, compact: TaskRunner | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
    ) -> SubmissionLoop:
        """Open the session-owned submission loop."""
        from .submission import SubmissionLoop

        if self.closed or self.submission_loop is not None:
            raise RuntimeError("session is closed or already has a submission loop")
        def emit(event_type: str, **payload: Any) -> dict[str, Any]:
            event = self.emit(event_type, **payload)
            if on_event is not None:
                on_event(event)
            return event

        self.submission_loop = SubmissionLoop(
            self.session_id, runner, emit, compact=compact, on_shutdown=self.close,
            owner_closed=lambda: self.closed,
        )
        return self.submission_loop

    @property
    def events(self) -> list[dict[str, Any]]:
        return self.recorder.history


    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        if self.active_turn_id is not None:
            payload.setdefault("turn_id", self.active_turn_id)
            if self.sampling_index:
                payload.setdefault("sampling_index", self.sampling_index)
        return self.recorder.emit(event_type, session_id=self.session_id, **payload)

    def hook(self, event: str, **payload: Any) -> Any:
        if self.hooks is None:
            return None
        return self.hooks.dispatch(event, self, **payload)

    def close(self) -> None:
        if self.submission_loop is not None and self.submission_loop.is_running and not self.submission_loop.is_closing:
            raise RuntimeError("close the active submission loop with Shutdown or cancellation")
        if self.closed:
            return
        failures: list[Exception] = []
        resources = [("subagents", lambda: self.subagents.close(wait=True))]
        if self.lsp_manager is not None:
            resources.append(("lsp", self.lsp_manager.shutdown_all))
        if self.mcp_manager is not None:
            resources.append(("mcp", self.mcp_manager.shutdown_all))
        executor = getattr(getattr(self.tools, "context", None), "executor", None)
        if executor is not None:
            resources.append(("executor", executor.terminate_all))
        try:
            for name, close in resources:
                try:
                    close()
                except Exception as exc:
                    failures.append(exc)
                    try:
                        self.emit("resource_cleanup_failed", resource=name,
                                  exception_type=type(exc).__name__, message=str(exc))
                    except Exception as audit_error:
                        failures.append(audit_error)
            self.closed = True
            self.emit("session_closed", cleanup_failed=bool(failures))
        finally:
            self.closed = True
            if not self.defer_lease_release:
                self.release_lease()
        if failures:
            raise ExceptionGroup("session resource cleanup failed", failures)


__all__ = ["Session"]
