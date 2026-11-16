"""Model/tool turn driver corresponding to Codex session/turn.rs responsibilities."""
from __future__ import annotations

import asyncio
import json
from typing import Callable, Any

from harness.protocol import TurnOutcome, TurnResult
from harness.runtime.recovery import RecoveryGuard
from harness.spec import HarnessSpec
from harness.tasks.base import join_on_cancel
from harness.types import ChatModel
from .budgets import _session_budget_reason
from .sampling import SamplingStep
from .sampling_support import _text
from .session import Session
from .submission import TurnControl
from .turn_context import TurnContext
from .interactions import ToolInteractionScope, TurnInteractions
from .boundaries import RuntimeBoundary, RestoredTurnPoint

ToolBatch = Callable[[Session, list[dict[str, Any]], int, Callable[[], bool]], None]


class TurnLoopDriver:
    def __init__(self, spec: HarnessSpec, model: ChatModel, execute_tools: ToolBatch, *,
                 interactive: bool = False,
                 control_poll: Callable[[TurnControl], None] | None = None):
        self.spec = spec
        self.sampling = SamplingStep(spec, model)
        self.execute_tools = execute_tools
        self._context: TurnContext | None = None
        self.interactive = interactive
        self.control_poll = control_poll
        self._restored: tuple[str, RestoredTurnPoint] | None = None
        self._restored_boundary: str | None = None
        self._restore_prepare: Callable[[Session], None] | None = None

    def restore_turn(self, session: Session, point: RestoredTurnPoint, *,
                     prepare: Callable[[Session], None] | None = None) -> None:
        """Install one joined boundary; never install pending tool/model work."""
        if (type(point) is not RestoredTurnPoint or session.closed or session.active_turn_id is not None
                or session.submission_loop is not None or self._context is not None or self._restored is not None):
            raise RuntimeError('turn restoration requires a fresh idle driver and session')
        if prepare is not None and not callable(prepare):
            raise TypeError('restore preparation must be a deployment-owned callable')
        self._restored = session.session_id, point
        self._restore_prepare = prepare
        session.emit('turn_restore_prepared', boundary=point.kind, steps=point.steps,
                     restored_sampling_index=point.sampling_index, authority_restored=False)

    def _check_active(self, control: TurnControl) -> None:
        if self.control_poll is not None:
            self.control_poll(control)
        control.check_active()

    async def _boundary(self, kind, session: Session, context: TurnContext, control: TurnControl):
        if session.boundary_handler is None:
            return
        self._check_active(control)
        boundary = RuntimeBoundary(kind, control.turn_id, context.steps,
                                   session.recorder.cursor, len(session.messages))
        await session.boundary_handler.at_boundary(session, boundary, context, control)
        self._check_active(control)

    async def _worker(self, function, context: TurnContext, *args):
        worker = asyncio.create_task(asyncio.to_thread(function, *args))
        return await join_on_cancel(worker, context.worker_cancelled.set)

    def _inputs(self, session: Session, control: TurnControl) -> bool:
        pending = control.inputs.drain()
        for item in pending:
            session.messages.append({"role": "user", "content": item.text})
        if pending:
            session.emit("regular_inputs_consumed",
                         operation_ids=[item.operation_id for item in pending])
        return bool(pending)

    def _budget(self, session: Session, context: TurnContext) -> str | None:
        reason = _session_budget_reason(session)
        maximum = session.orchestrator.max_turns
        if maximum is not None and context.steps >= maximum:
            reason = "explicit sampling-step budget exhausted"
        if session.tool_call_budget is not None and sum(
            event["type"] == "tool" for event in session.events
        ) >= session.tool_call_budget:
            reason = "explicit tool-call budget exhausted"
        return reason

    def _queue_unsupported_output_notice(self, session: Session, context: TurnContext,
                                         response: dict) -> bool:
        kinds = list(response.get("_unsupported_output_items") or [])
        if not kinds:
            return False
        context.unsupported_output_notices += 1
        session.emit("model_output_unsupported",
                     item_types=kinds,
                     sampling_index=context.steps,
                     notice_index=context.unsupported_output_notices,
                     action="prompt_model_to_retry")
        if context.unsupported_output_notices > 2:
            return False
        session.messages.append({
            "role": "user",
            "content": (
                "Harness protocol notice: the previous response contained unsupported "
                "Responses output item type(s): " + ", ".join(kinds) + ". They were not "
                "executed or replayed. Continue with only supported response items."
            ),
        })
        return True

    def _unsupported_output_error(self, session: Session, context: TurnContext,
                                  response: dict) -> TurnResult:
        kinds = ", ".join(response.get("_unsupported_output_items") or [])
        return TurnResult(session.session_id, self._context.turn_id if self._context else "",
                          TurnOutcome.ERROR,
                          error="unsupported Responses output items persisted: " + kinds)

    def _finish_tool_group(self, session: Session, calls: list[dict], start: int) -> None:
        returned = {message.get("tool_call_id") for message in session.messages[start:]
                    if message.get("role") == "tool"}
        for call in calls:
            if call["id"] not in returned:
                # Execution may have happened before the callback failed. Do
                # not claim an unreported side effect was absent or replay it.
                session.messages.append({
                    "role": "tool", "tool_call_id": call["id"],
                    "content": json.dumps({"ok": False, "failure_kind": "tool_result_unavailable",
                                           "execution_status": "unknown", "retry_automatically": False}),
                })
                session.emit("tool_result_unavailable", tool_call_id=call["id"],
                             execution_status="unknown", retry_automatically=False)

    async def run_turn(self, session: Session, control: TurnControl) -> TurnResult:
        if self._restored is not None:
            owner, point = self._restored
            if owner != session.session_id:
                raise RuntimeError('restored turn belongs to another session')
            self._context = TurnContext(control.turn_id, steps=point.steps,
                                        unsupported_output_notices=point.unsupported_output_notices)
            session.sampling_index = point.sampling_index
            self._restored_boundary = point.kind
            self._restored = None
            session.emit('turn_restored', boundary=point.kind, steps=point.steps,
                         restored_sampling_index=point.sampling_index, authority_restored=False)
        elif self._context is None or self._context.turn_id != control.turn_id:
            self._context = TurnContext(control.turn_id)
            session.sampling_index = 0
            session.state.final_text = ""
            session.recovery = RecoveryGuard(self.spec.recovery)
        interaction = TurnInteractions(control, self._context.worker_cancelled,
                                       interactive=self.interactive,
                                       preapprove_tool_hooks=getattr(
                                           session.tools.permission_policy, "preapprove_tool_hooks", False))
        with ToolInteractionScope(session.tools, interaction, allow_user_input=(
            session.parent_session_id is None and session.agent_role == "parent"
        )):
            return await self._run_turn(session, control)

    async def _run_turn(self, session: Session, control: TurnControl) -> TurnResult:
        context = self._context
        assert context is not None
        background = session.tools.context.background_tasks
        session.tools.context.interrupt_wait = (
            (lambda: context.worker_cancelled.is_set() or background.changed())
            if background is not None else context.worker_cancelled.is_set
        )
        try:
            restored_boundary, self._restored_boundary = self._restored_boundary, None
            if self._restore_prepare is not None:
                prepare, self._restore_prepare = self._restore_prepare, None
                prepare(session)
            if (restored_boundary == 'final_response' and not self._inputs(session, control)
                    and (not session.messages or session.messages[-1].get('role') != 'user')):
                session.emit('turn_returned', verification='not_evaluated', restored_final_response=True)
                return TurnResult(session.session_id, control.turn_id, TurnOutcome.COMPLETED,
                                  session.state.final_text)
            if context.steps == 0 and restored_boundary is None:
                await self._boundary("initial", session, context, control)
            resume_at_model = restored_boundary == 'before_model'
            while True:
                self._check_active(control)
                if not resume_at_model:
                    reason = self._budget(session, context)
                    if reason:
                        session.emit("budget", reason=reason)
                        return TurnResult(session.session_id, control.turn_id, TurnOutcome.BUDGET_LIMITED,
                                          session.state.final_text or None)
                    self._inputs(session, control)
                    await self._boundary("before_model", session, context, control)
                resume_at_model = False
                context.steps += 1
                session.state.turn = context.steps  # legacy event consumers
                sampled = await self._worker(
                    self.sampling.sample, context, session, context.worker_cancelled.is_set,
                )
                self._check_active(control)
                if sampled.termination is not None:
                    if sampled.termination == "budget_exhausted":
                        return TurnResult(session.session_id, control.turn_id, TurnOutcome.BUDGET_LIMITED)
                    if sampled.termination == "cancellation_requested":
                        return TurnResult(session.session_id, control.turn_id, TurnOutcome.INTERRUPTED)
                    return TurnResult(session.session_id, control.turn_id, TurnOutcome.ERROR,
                                      error="; ".join(sampled.reasons) or sampled.termination)
                if sampled.retry:
                    continue
                response = sampled.response
                assert response is not None
                calls = response.get("tool_calls") or []
                identifiers = [call.get("id") for call in calls]
                if any(not isinstance(value, str) or not value for value in identifiers) or len(set(identifiers)) != len(identifiers):
                    raise ValueError("tool calls require unique nonempty IDs")
                group_start = len(session.messages)
                session.messages.append({key: value for key, value in response.items()
                                         if not key.startswith("_")})
                if calls:
                    try:
                        await self._worker(self.execute_tools, context, session, calls,
                                           context.steps, context.worker_cancelled.is_set)
                    finally:
                        self._finish_tool_group(session, calls, group_start)
                    self._check_active(control)
                    if response.get("_unsupported_output_items"):
                        if not self._queue_unsupported_output_notice(session, context, response):
                            return self._unsupported_output_error(session, context, response)
                    await self._boundary("after_tools", session, context, control)
                    continue
                if self._queue_unsupported_output_notice(session, context, response):
                    continue
                if response.get("_unsupported_output_items"):
                    return self._unsupported_output_error(session, context, response)
                text = _text(response.get("content"))
                session.state.final_text = text
                session.emit("assistant_message", text=text)
                await self._boundary("final_response", session, context, control)
                # Incoming steering is consumed only after complete tool groups.
                # Final text is a turn boundary, never an assertion of success.
                if self._inputs(session, control):
                    continue
                session.emit("turn_returned", verification="not_evaluated")
                return TurnResult(session.session_id, control.turn_id, TurnOutcome.COMPLETED, text)
        except asyncio.CancelledError:
            context.worker_cancelled.set()
            raise
        except Exception as exc:
            session.emit("turn_error", exception_type=type(exc).__name__, message=str(exc))
            return TurnResult(session.session_id, control.turn_id, TurnOutcome.ERROR,
                              error=f"{type(exc).__name__}: {exc}")
