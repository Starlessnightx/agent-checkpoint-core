from __future__ import annotations

import asyncio
from copy import deepcopy
from typing import Awaitable, Callable
from uuid import uuid4

from harness.core.runtime import AgentRuntime
from harness.core.turn import TurnLoopDriver
from harness.core.assembly import AssemblyProfile
from harness.protocol import ApprovalResponse, EventKind, Interrupt, RequestRef, Shutdown, UserInputAnswer, UserTurn
from harness.tasks.base import join_on_cancel
from harness.tasks.regular import RegularTask
from harness.types import AgentResult

RequestHandler = Callable[[dict], Awaitable[ApprovalResponse | UserInputAnswer | None]]


class Conversation:
    """Frontend lifetime for one Session, independent of a single user turn."""
    def __init__(self, runtime: AgentRuntime, task: str, *, handler: RequestHandler):
        self.owner = asyncio.get_running_loop()
        self.session = runtime.create_session(task, profile=AssemblyProfile.TURN)
        self.handler = handler
        self.inbox: asyncio.Queue[dict] = asyncio.Queue()
        driver = TurnLoopDriver(runtime.spec, runtime.model, runtime.execute_tool_calls, interactive=True)
        self.loop = self.session.open_submission_loop(RegularTask(self.session, driver), on_event=self._publish)
        self.consumer: asyncio.Task | None = None
        self.responder: asyncio.Task | None = None
        self.frontend_error: Exception | None = None
        self.reply_task: asyncio.Task | None = None
        self.active_request: dict | None = None
        self.closed_turns: set[str] = set()
        self.cancelled_requests: set[str] = set()

    def _publish(self, event: dict) -> None:
        if event["type"] in {"approval_requested", "user_input_requested"}:
            self.owner.call_soon_threadsafe(self.inbox.put_nowait, deepcopy(event))
        elif event["type"] in {"request_cancelled", "turn_aborted", "turn_completed"}:
            self.owner.call_soon_threadsafe(self._cancel_prompt, dict(event))

    def _cancel_prompt(self, event: dict) -> None:
        if event["type"] == "request_cancelled":
            self.cancelled_requests.add(event["request_id"])
        else:
            self.closed_turns.add(event["turn_id"])
        if (self.active_request is not None and self.reply_task is not None
                and self.active_request["turn_id"] == event.get("turn_id")
                and (event["type"] != "request_cancelled"
                     or self.active_request["request_id"] == event.get("request_id"))):
            if not self.reply_task.cancelling():
                self.reply_task.cancel()

    async def __aenter__(self) -> Conversation:
        self.consumer = asyncio.create_task(self.loop.run())
        self.responder = asyncio.create_task(self._respond())
        return self

    async def _respond(self) -> None:
        while True:
            event = await self.inbox.get()
            if event["turn_id"] in self.closed_turns or event["request_id"] in self.cancelled_requests:
                continue
            try:
                self.active_request = event
                self.reply_task = asyncio.create_task(self.handler(event))
                reply = await self.reply_task
                if reply is None:
                    self.loop.submit(Interrupt(session_id=self.session.session_id,
                        operation_id=str(uuid4()), turn_id=event["turn_id"]))
                else:
                    # Never allow a frontend to answer a different request.
                    RequestRef(self.session.session_id, event["turn_id"], event["request_id"],
                               EventKind(event["type"])).validate_reply(reply)
                    if isinstance(reply, UserInputAnswer) and {key for key, _ in reply.answers} != {
                        question["id"] for question in event["details"]["questions"]
                    }:
                        raise ValueError("frontend answer IDs do not match questions")
                    self.loop.submit(reply)
            except asyncio.CancelledError:
                if asyncio.current_task().cancelling():
                    raise
            except Exception as exc:
                self.frontend_error = exc
                self.session.emit("frontend_error", exception_type=type(exc).__name__, message=str(exc))
                self.loop.submit(Interrupt(session_id=self.session.session_id,
                    operation_id=str(uuid4()), turn_id=event["turn_id"]))
                return
            finally:
                self.reply_task, self.active_request = None, None

    async def turn(self, text: str) -> AgentResult:
        if self.consumer is None or self.consumer.done():
            raise RuntimeError("conversation is not running")
        turn_id = str(uuid4())
        self.loop.submit(UserTurn(session_id=self.session.session_id, operation_id=str(uuid4()),
                                  turn_id=turn_id, text=text))
        result = None
        try:
            result = await self.loop.wait_result(turn_id)
            prompt = self.reply_task
            if prompt is not None and self.active_request is not None and self.active_request["turn_id"] == turn_id:
                if not prompt.cancelling():
                    prompt.cancel()
                await asyncio.gather(prompt, return_exceptions=True)
            if self.frontend_error is not None:
                raise RuntimeError(f"frontend failed: {self.frontend_error}") from self.frontend_error
            reasons = (result.error,) if result.error else (
                ("verification not evaluated",) if result.outcome.value == "completed" else (result.outcome.value,))
            return AgentResult(result.last_agent_message or "", False, reasons, self.session.events)
        except asyncio.CancelledError:
            if self.consumer is not None and not self.consumer.done():
                self.loop.submit(Interrupt(session_id=self.session.session_id,
                    operation_id=str(uuid4()), turn_id=turn_id))
                waiter = asyncio.create_task(self.loop.wait_result(turn_id))
                await join_on_cancel(waiter, lambda: None)
            raise
        finally:
            self.session.emit("run_end", turn_id=turn_id,
                termination="error" if self.frontend_error else (result.outcome.value if result else "interrupted"),
                verified=False, verification_status="not_evaluated", execution_path="interactive_turn_loop",
                session_usage=self.session.usage.dictionary(), task_usage=self.session.task_usage.snapshot())

    async def __aexit__(self, *exc) -> None:
        cleanup = asyncio.create_task(self._close())
        await join_on_cancel(cleanup, lambda: None)

    async def _close(self) -> None:
        if self.responder is not None:
            self.responder.cancel()
            await asyncio.gather(self.responder, return_exceptions=True)
        if self.consumer is not None:
            if not self.consumer.done():
                self.loop.submit(Shutdown(session_id=self.session.session_id, operation_id=str(uuid4())))
            await join_on_cancel(self.consumer, lambda: None)
