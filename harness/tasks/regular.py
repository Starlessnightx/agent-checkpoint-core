"""RegularTask owns a user request; its driver owns the inner model/tool loop."""
from __future__ import annotations
from dataclasses import replace

from harness.core.session import Session
from harness.core.submission import TurnControl
from harness.protocol import Compact, EventKind, Steer, TurnOutcome, TurnResult, UserTurn
from .base import TurnDriver


class RegularTask:
    def __init__(self, session: Session, driver: TurnDriver, *, restored: bool = False,
                 restored_inputs: tuple[Steer, ...] = ()):
        if (type(restored) is not bool or type(restored_inputs) is not tuple
                or any(type(item) is not Steer for item in restored_inputs)
                or restored_inputs and not restored):
            raise ValueError('saved turn inputs require explicit restored task mode')
        self.session, self.driver = session, driver
        self._initial_consumed = False
        self._running = False
        self._restored_pending = restored
        self._restored_inputs = restored_inputs

    async def __call__(self, operation: UserTurn | Compact, control: TurnControl) -> TurnResult:
        if type(operation) is not UserTurn:
            raise ValueError("RegularTask requires a UserTurn")
        if self._running or self.session.closed:
            raise RuntimeError("regular task is already running or session is closed")
        if (operation.session_id, operation.turn_id) != (self.session.session_id, control.turn_id):
            raise ValueError("regular task scope mismatch")
        control.check_active()
        self._running = True
        self.session.active_turn_id = operation.turn_id
        self.session.state.status = "running"
        try:
            if not self._restored_pending:
                self._consume_deferred()
            # create_session already records its first user request. Subsequent
            # turns append new input; never replace the conversation prefix.
            if self._restored_pending:
                if operation.text != self.session.task.text:
                    raise ValueError('restored task start must reference the original task; use steering for new input')
                for item in self._restored_inputs:
                    control.inputs.put(replace(item, session_id=self.session.session_id, turn_id=control.turn_id))
                if self._restored_inputs:
                    self.session.emit('checkpoint_turn_inputs_bound',
                        operation_ids=[item.operation_id for item in self._restored_inputs],
                        source_turn_ids=[item.turn_id for item in self._restored_inputs])
                self._restored_inputs = ()
                self._restored_pending = False
            elif self._initial_consumed or operation.text != self.session.task.text:
                self.session.messages.append({"role": "user", "content": operation.text})
            self._initial_consumed = True
            self.session.emit("regular_task_started", operation_id=operation.operation_id)
            while True:
                control.check_active()
                result = await self.driver.run_turn(self.session, control)
                if not isinstance(result, TurnResult) or (result.session_id, result.turn_id) != (
                    self.session.session_id, operation.turn_id
                ):
                    raise ValueError("turn driver returned mismatched result")
                control.check_active()
                pending = control.inputs.drain()
                if result.outcome is not TurnOutcome.COMPLETED:
                    self._defer(pending, "terminal turn result")
                    return result
                if not pending:
                    return result
                for item in pending:
                    self.session.messages.append({"role": "user", "content": item.text})
                self.session.emit("regular_inputs_consumed",
                                  operation_ids=[item.operation_id for item in pending])
        finally:
            self._defer(control.inputs.drain(), "task exited before consuming input")
            self.session.emit("regular_task_finished")
            self.session.active_turn_id = None
            if not self.session.closed:
                self.session.state.status = "idle"
            self._running = False

    def _defer(self, inputs: tuple[Steer, ...], reason: str) -> None:
        if inputs:
            self.session.deferred_inputs.extend(inputs)
            self.session.emit(EventKind.INPUT_UNCONSUMED.value,
                              operation_ids=[item.operation_id for item in inputs],
                              reason=reason, retained_in_session=True)

    def _consume_deferred(self) -> None:
        pending = tuple(self.session.deferred_inputs)
        if not pending:
            return
        if any(item.session_id != self.session.session_id for item in pending):
            raise ValueError("deferred input belongs to another session")
        self.session.emit("deferred_inputs_consumed",
                          operation_ids=[item.operation_id for item in pending],
                          source_turn_ids=[item.turn_id for item in pending])
        self.session.messages.extend({"role": "user", "content": item.text} for item in pending)
        self.session.deferred_inputs.clear()
