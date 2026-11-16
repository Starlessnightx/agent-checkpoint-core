"""Adapt a coordinator's control mailbox to typed submission operations."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
from uuid import uuid4

from harness.protocol import Interrupt, Steer
from harness.types import AgentResult
from .oneshot import _run_session_once


async def run_delegated(runtime, session, control: Callable[[], dict]):
    initial = control()
    if initial.get("cancel"):
        session.state.termination = "cancellation_requested"
        session.emit("delegated_cancelled_before_start")
        session.close()
        session.emit("run_end", termination="interrupted", verified=False,
                     reasons=["cancelled before start"], verification_status="not_evaluated",
                     execution_path="submission_turn_loop", session_usage=session.usage.dictionary(),
                     task_usage=session.task_usage.snapshot())
        return AgentResult("", False, ("cancelled before start",), session.events)
    for text in initial.get("steering", ()):
        session.messages.append({"role": "user", "content": str(text)})
        session.emit("delegated_input_received", text=str(text), before_start=True)

    async def forward():
        while not runner.done():
            loop = session.submission_loop
            turn_id = session.active_turn_id
            if loop is not None and turn_id is not None and not loop.is_closing:
                incoming = control()
                for text in incoming.get("steering", ()):
                    loop.submit(Steer(session_id=session.session_id, operation_id=uuid4().hex,
                                      turn_id=turn_id, text=str(text)))
                if incoming.get("cancel"):
                    loop.submit(Interrupt(session_id=session.session_id, operation_id=uuid4().hex,
                                          turn_id=turn_id))
                    return
            # Control-channel polling only; never samples the model to wait.
            await asyncio.sleep(0.05)

    def poll_boundary(turn_control):
        # A request may return before the polling timer wakes. Check cancellation
        # at every model/tool boundary so a late response cannot start side effects.
        incoming = control()
        for text in incoming.get("steering", ()):
            operation = Steer(session_id=session.session_id, operation_id=uuid4().hex,
                              turn_id=turn_control.turn_id, text=str(text))
            turn_control.inputs.put(operation)
            session.emit("delegated_input_received", text=str(text),
                         operation_id=operation.operation_id, before_start=False)
        if incoming.get("cancel"):
            session.emit("delegated_cancellation_received")
            turn_control.cancelled.set()

    runner = asyncio.create_task(_run_session_once(runtime, session, session.task.text,
                                                   control_poll=poll_boundary))
    forwarding = asyncio.create_task(forward())
    try:
        done, _ = await asyncio.wait((runner, forwarding), return_when=asyncio.FIRST_COMPLETED)
        if forwarding in done:
            forwarding.result()  # Propagate mailbox errors; cleanup joins workers.
        return await runner
    finally:
        forwarding.cancel()
        await asyncio.gather(forwarding, return_exceptions=True)
        if not runner.done():
            runner.cancel()
        await asyncio.gather(runner, return_exceptions=True)
