"""Opt-in Codex-style operation consumer, independent of model/tool policies."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol

from harness.protocol import (
    ApprovalResponse, Compact, EventKind, Interrupt, Operation, RequestRef,
    Shutdown, Steer, TurnOutcome, TurnResult, UserInputAnswer, UserTurn,
)
from .input_queue import InputQueue, TurnInputQueue
from .pending_requests import PendingRequests, Reply

Emit = Callable[..., object]


@dataclass
class TurnControl:
    session_id: str
    turn_id: str
    inputs: TurnInputQueue
    cancelled: asyncio.Event
    _requests: PendingRequests
    _emit: Emit
    closed: bool = False

    @property
    def pending_request_count(self) -> int:
        return self._requests.pending_count

    def check_active(self) -> None:
        if self.closed or self.cancelled.is_set():
            raise asyncio.CancelledError()

    async def request(self, request_id: str, kind: EventKind, text: str, *,
                      details: dict | None = None,
                      answer_ids: frozenset[str] | None = None) -> Reply:
        self.check_active()
        ref = RequestRef(self.session_id, self.turn_id, request_id, kind)
        future = self._requests.register(ref, answer_ids=answer_ids)
        try:
            self._emit(kind.value, turn_id=self.turn_id, request_id=request_id, text=text,
                       details=details or {})
            return await future
        finally:
            if self._requests.cancel(request_id) is not None:
                self._emit(EventKind.REQUEST_CANCELLED.value, turn_id=self.turn_id,
                           request_id=request_id)


class TaskRunner(Protocol):
    def __call__(self, operation: UserTurn | Compact, control: TurnControl) -> Awaitable[TurnResult]: ...


class SubmissionLoop:
    def __init__(self, session_id: str, runner: TaskRunner, emit: Emit, *,
                 compact: TaskRunner | None = None,
                 on_shutdown: Callable[[], None] | None = None,
                 owner_closed: Callable[[], bool] | None = None):
        self.session_id, self.runner, self.emit = session_id, runner, emit
        self.compact, self.on_shutdown = compact, on_shutdown
        self.owner_closed = owner_closed
        self.input_queue = InputQueue(session_id)
        self.requests = PendingRequests(session_id)
        self._active: asyncio.Task[TurnResult] | None = None
        self._control: TurnControl | None = None
        self._active_regular = False
        self._started = self._closing = self._finished = False
        self._turn_ids: set[str] = set()
        self._submitted_turns: set[str] = set()
        self._results: dict[str, TurnResult] = {}
        self._rejections: dict[str, str] = {}
        self._result_changed = asyncio.Event()
        self._undispatched: dict[str, Operation] = {}

    @property
    def is_running(self) -> bool:
        return self._started and not self._finished

    @property
    def is_closing(self) -> bool:
        return self._closing

    @property
    def pending_inputs(self) -> tuple[Steer, ...]:
        return self._control.inputs.snapshot() if self._control is not None else ()

    @property
    def pending_operations(self) -> tuple[Operation, ...]:
        """Include operations already held by receive(), before dispatch begins."""
        return tuple(self._undispatched.values())

    def submit(self, operation: Operation) -> None:
        try:
            if self.owner_closed is not None and self.owner_closed():
                raise RuntimeError("owning session is closed")
            self.input_queue.validate(operation)
        except (ValueError, RuntimeError) as exc:
            self.emit(EventKind.SUBMISSION_REJECTED.value,
                      operation_id=getattr(operation, "operation_id", None), reason=str(exc))
            raise
        # Audit the input before making it available for dispatch. No await
        # separates validation, recording and enqueueing in this loop.
        self.emit(EventKind.SUBMISSION_RECEIVED.value, operation=operation.dictionary())
        self.input_queue.submit(operation)
        self._undispatched[operation.operation_id] = operation
        if type(operation) is UserTurn:
            self._submitted_turns.add(operation.turn_id)

    async def wait_result(self, turn_id: str) -> TurnResult:
        if turn_id not in self._submitted_turns:
            raise ValueError("turn has not been submitted")
        while True:
            self._result_changed.clear()
            if turn_id in self._results:
                return self._results[turn_id]
            if turn_id in self._rejections:
                raise ValueError(self._rejections[turn_id])
            if self._finished:
                raise RuntimeError("session shut down before turn started")
            await self._result_changed.wait()

    async def run(self) -> None:
        if self._started:
            raise RuntimeError("submission loop can run only once")
        if self.owner_closed is not None and self.owner_closed():
            raise RuntimeError("owning session is closed")
        self._started = True
        incoming = asyncio.create_task(self.input_queue.receive())
        try:
            while True:
                waiters = {incoming}
                if self._active is not None:
                    waiters.add(self._active)
                await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                # Reap before accepting targeted operations so a finished task
                # cannot receive a reply or steer for an already closed turn.
                if self._active is not None and self._active.done():
                    await self._finish_active()
                if incoming.done():
                    operation = incoming.result()
                    if operation is None:
                        break
                    self._undispatched.pop(operation.operation_id, None)
                    if isinstance(operation, Shutdown):
                        self.emit(EventKind.SUBMISSION_ACCEPTED.value,
                                  operation_id=operation.operation_id)
                        break
                    try:
                        await self._dispatch(operation)
                    except ValueError as exc:
                        if type(operation) is UserTurn and operation.turn_id not in self._turn_ids:
                            self._rejections[operation.turn_id] = str(exc)
                            self._result_changed.set()
                        self.emit(EventKind.SUBMISSION_REJECTED.value,
                                  operation_id=operation.operation_id, reason=str(exc))
                    else:
                        self.emit(EventKind.SUBMISSION_ACCEPTED.value,
                                  operation_id=operation.operation_id)
                    incoming = asyncio.create_task(self.input_queue.receive())
        finally:
            self._closing = True
            # The receive task may already hold a not-yet-dispatched operation.
            if incoming.done() and not incoming.cancelled():
                held = incoming.result()
                if held is not None and not isinstance(held, Shutdown):
                    self.emit(EventKind.SUBMISSION_REJECTED.value,
                              operation_id=held.operation_id, reason="session shutting down")
            incoming.cancel()
            await asyncio.gather(incoming, return_exceptions=True)
            for discarded in self.input_queue.close():
                self.emit(EventKind.SUBMISSION_REJECTED.value,
                          operation_id=discarded.operation_id, reason="session shutting down")
            self._undispatched.clear()
            try:
                await self._interrupt()
            finally:
                try:
                    if self.on_shutdown is not None:
                        self.on_shutdown()
                finally:
                    self._finished = True
                    self._result_changed.set()
                    self.emit(EventKind.SESSION_SHUTDOWN.value)

    async def _dispatch(self, operation: Operation) -> None:
        if isinstance(operation, Steer):
            control = self._target(operation.turn_id)
            if not self._active_regular:
                raise ValueError("active task does not accept steering")
            control.inputs.put(operation)
            self.emit(EventKind.INPUT_STEERED.value, turn_id=control.turn_id,
                      operation_id=operation.operation_id)
        elif isinstance(operation, UserTurn):
            self._start(operation, self.runner, operation.turn_id, regular=True)
        elif isinstance(operation, Compact):
            if self.compact is None:
                raise ValueError("compact task handler is not configured")
            self._start(operation, self.compact, "compact:" + operation.operation_id, regular=False)
        elif isinstance(operation, Interrupt):
            self._target(operation.turn_id)
            await self._interrupt()
        elif isinstance(operation, (ApprovalResponse, UserInputAnswer)):
            self._target(operation.turn_id)
            self.requests.resolve(operation)
            self.emit(EventKind.REQUEST_RESOLVED.value, turn_id=operation.turn_id,
                      request_id=operation.request_id, reply=operation.dictionary())
        else:
            raise ValueError("unsupported operation")

    def _target(self, turn_id: str) -> TurnControl:
        if self._control is None or self._control.turn_id != turn_id or self._control.closed:
            raise ValueError("target turn is not active")
        return self._control

    def _start(self, operation: UserTurn | Compact, runner: TaskRunner,
               turn_id: str, *, regular: bool) -> None:
        if self._active is not None:
            raise ValueError("session is busy; use exact-turn steering or interrupt first")
        if turn_id in self._turn_ids or turn_id in self._rejections:
            raise ValueError("turn ID already used")
        self._turn_ids.add(turn_id)
        control = TurnControl(self.session_id, turn_id, TurnInputQueue(), asyncio.Event(),
                              self.requests, self.emit)
        self._control, self._active_regular = control, regular
        self.emit(EventKind.TURN_STARTED.value, turn_id=turn_id,
                  task_kind="regular" if regular else "compact")
        self._active = asyncio.create_task(self._execute(runner, operation, control))

    async def _execute(self, runner: TaskRunner, operation: UserTurn | Compact,
                       control: TurnControl) -> TurnResult:
        result = await runner(operation, control)
        if not isinstance(result, TurnResult) or (
            result.session_id, result.turn_id
        ) != (self.session_id, control.turn_id):
            raise ValueError("task returned a mismatched turn result")
        if control.inputs.has_pending_input:
            # Step 4's RegularTask must consume pending input before returning.
            # Preserve its original submission in the audit, never claim success.
            self.emit(EventKind.INPUT_UNCONSUMED.value, turn_id=control.turn_id)
            raise ValueError("task returned with unconsumed steering input")
        return result

    async def _interrupt(self) -> None:
        if self._active is None:
            return
        assert self._control is not None
        self._control.cancelled.set()
        self._active.cancel()
        await self._finish_active()

    async def _finish_active(self) -> None:
        assert self._active is not None and self._control is not None
        task, control = self._active, self._control
        try:
            result = await task
        except asyncio.CancelledError:
            result = TurnResult(self.session_id, control.turn_id, TurnOutcome.INTERRUPTED)
        except Exception as exc:
            result = TurnResult(self.session_id, control.turn_id, TurnOutcome.ERROR,
                                error=f"{type(exc).__name__}: {exc}")
        if control.cancelled.is_set():
            result = TurnResult(self.session_id, control.turn_id, TurnOutcome.INTERRUPTED)
        control.closed = True
        for ref in self.requests.cancel_turn(control.turn_id):
            self.emit(EventKind.REQUEST_CANCELLED.value, turn_id=control.turn_id,
                      request_id=ref.request_id)
        kind = EventKind.TURN_COMPLETED if result.outcome is TurnOutcome.COMPLETED else EventKind.TURN_ABORTED
        self._active, self._control = None, None
        self.emit(kind.value, turn_id=result.turn_id, result=result.dictionary())
        self._results[result.turn_id] = result
        self._result_changed.set()
