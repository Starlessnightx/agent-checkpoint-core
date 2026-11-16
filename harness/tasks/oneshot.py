"""One-shot caller adapter over Session/submission/task execution."""
from __future__ import annotations

import asyncio
from uuid import uuid4

from harness.core.runtime import AgentRuntime
from harness.core.task import TaskContract
from harness.core.turn import TurnLoopDriver
from harness.core.metrics import behavior_summary
from harness.protocol import Shutdown, TurnOutcome, UserTurn
from harness.types import AgentResult
from .base import join_on_cancel
from .regular import RegularTask
from harness.core.assembly import AssemblyProfile


async def run_once(runtime: AgentRuntime, task: str | TaskContract) -> AgentResult:
    session = runtime.create_session(task, profile=AssemblyProfile.TURN)
    session.defer_lease_release = True
    try:
        return await _run_session_once(runtime, session, task if isinstance(task, str) else task.text)
    finally:
        session.release_lease()


async def _run_session_once(runtime, session, task: str, *, control_poll=None) -> AgentResult:
    driver = TurnLoopDriver(runtime.spec, session.model or runtime.model, runtime.execute_tool_calls,
                            control_poll=control_poll)
    execution_path = "submission_turn_loop"
    loop = session.open_submission_loop(RegularTask(session, driver))
    consumer = asyncio.create_task(loop.run())
    turn_id = str(uuid4())
    result = None
    cancelled = False
    try:
        loop.submit(UserTurn(session_id=session.session_id, operation_id=str(uuid4()),
                             turn_id=turn_id, text=task))
        result = await loop.wait_result(turn_id)
    except asyncio.CancelledError:
        cancelled = True
        raise
    finally:
        try:
            if not consumer.done():
                loop.submit(Shutdown(session_id=session.session_id, operation_id=str(uuid4())))
            await join_on_cancel(consumer, lambda: None)
        finally:
            interrupted = cancelled or bool(asyncio.current_task().cancelling())
            if result is None or interrupted:
                session.emit("run_end", termination="interrupted" if interrupted else "error",
                             behavior_summary=behavior_summary(session.events),
                             verified=False, reasons=["one-shot caller cancelled" if interrupted else "one-shot execution failed"],
                             session_usage=session.usage.dictionary(),
                             task_usage=session.task_usage.snapshot() if session.task_usage else session.usage.dictionary(),
                             execution_path=execution_path)
    assert result is not None
    verified = False
    reasons = ((result.error,) if result.error else
               (("verification not evaluated",) if result.outcome is TurnOutcome.COMPLETED else (result.outcome.value,)))
    session.emit("run_end", termination=result.outcome.value, verified=verified,
                 behavior_summary=behavior_summary(session.events),
                 reasons=list(reasons), session_usage=session.usage.dictionary(),
                 task_usage=session.task_usage.snapshot() if session.task_usage else session.usage.dictionary(),
                 execution_path=execution_path, verification_status="not_evaluated")
    return AgentResult(result.last_agent_message or "", verified, reasons, session.events)
