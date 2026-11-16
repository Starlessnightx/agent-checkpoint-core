"""Async channel/task integration: no models, tools, network or paid requests."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest

from harness.core import AgentRuntime
from harness.core.input_queue import InputQueue
from harness.core.pending_requests import PendingRequests
from harness.core.submission import SubmissionLoop
from harness.protocol import (
    ApprovalResponse, Compact, EventKind, Interrupt, RequestRef, Shutdown, Steer,
    TurnOutcome, TurnResult, UserInputAnswer, UserTurn,
)
from harness.spec import build_spec

ROOT = Path(__file__).resolve().parents[1]


class Events:
    def __init__(self, forward=None):
        self.items = []
        self.changed = asyncio.Event()
        self.forward = forward

    def __call__(self, kind, **payload):
        self.items.append({"type": kind, **payload})
        if self.forward:
            self.forward(kind, **payload)
        self.changed.set()

    async def wait(self, kind, **match):
        async def find():
            while True:
                self.changed.clear()
                for item in self.items:
                    if item["type"] == kind and all(item.get(k) == v for k, v in match.items()):
                        return item
                await self.changed.wait()
        return await asyncio.wait_for(find(), 2)


@asynccontextmanager
async def running(loop):
    consumer = asyncio.create_task(loop.run())
    try:
        yield consumer
    finally:
        if not consumer.done():
            loop.submit(Shutdown(session_id=loop.session_id, operation_id="test-cleanup"))
        await asyncio.wait_for(consumer, 2)


def user(turn="t", op="start", session="s"):
    return UserTurn(session_id=session, operation_id=op, turn_id=turn, text="Inspect project")


class SubmissionLoopTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_steering_is_consumed_once_and_next_turn_uses_same_session(self):
        events = Events()
        observed = []

        async def runner(operation, control):
            if operation.turn_id == "t":
                await control.inputs.wait()
                observed.extend(control.inputs.drain())
                self.assertFalse(control.inputs.has_pending_input)
            return TurnResult("s", operation.turn_id, TurnOutcome.COMPLETED, "Cannot finish yet.")

        loop = SubmissionLoop("s", runner, events)
        async with running(loop):
            loop.submit(user())
            await events.wait("turn_started")
            loop.submit(Steer(session_id="s", operation_id="guide", turn_id="t", text="Use small sample"))
            first = await events.wait("turn_completed", turn_id="t")
            self.assertNotIn("verified", first["result"])
            loop.submit(user("second", "start-second"))
            await events.wait("turn_completed", turn_id="second")
        self.assertEqual([value.text for value in observed], ["Use small sample"])
        self.assertEqual(len([e for e in events.items if e["type"] == "turn_started"]), 2)

    async def test_wrong_kind_target_and_duplicate_reply_do_not_release_waiter_twice(self):
        events = Events()
        replies = []

        async def runner(operation, control):
            replies.append(await control.request("permission", EventKind.APPROVAL_REQUESTED, "Approve?"))
            return TurnResult("s", "t", TurnOutcome.COMPLETED)

        loop = SubmissionLoop("s", runner, events)
        async with running(loop):
            loop.submit(user())
            await events.wait("approval_requested")
            base = dict(session_id="s", turn_id="t", request_id="permission")
            wrong = UserInputAnswer(**base, operation_id="wrong-kind", answers=(("q", "yes"),))
            loop.submit(wrong)
            await events.wait("submission_rejected", operation_id="wrong-kind")
            loop.submit(ApprovalResponse(**base, operation_id="deny", approved=False))
            loop.submit(ApprovalResponse(**base, operation_id="replay", approved=True))
            await events.wait("submission_rejected", operation_id="replay")
            await events.wait("turn_completed")
        self.assertEqual(len(replies), 1)
        self.assertFalse(replies[0].approved)
        self.assertEqual(len([e for e in events.items if e["type"] == "request_resolved"]), 1)

    async def test_user_answers_are_delivered_during_active_task(self):
        events = Events()
        answers = []

        async def runner(operation, control):
            reply = await control.request("question", EventKind.USER_INPUT_REQUESTED, "Which target?")
            answers.extend(reply.answers)
            return TurnResult("s", "t", TurnOutcome.COMPLETED)

        loop = SubmissionLoop("s", runner, events)
        async with running(loop):
            loop.submit(user())
            await events.wait("user_input_requested")
            loop.submit(UserInputAnswer(session_id="s", operation_id="answer", turn_id="t",
                                        request_id="question", answers=(("target", "tests"),)))
            await events.wait("turn_completed")
        self.assertEqual(answers, [("target", "tests")])

    async def test_interrupt_cancels_request_and_stale_reply_does_not_affect_next_turn(self):
        events = Events()
        completed = []

        async def runner(operation, control):
            await control.request("permission-" + operation.turn_id, EventKind.APPROVAL_REQUESTED, "Approve?")
            completed.append(operation.turn_id)
            return TurnResult("s", operation.turn_id, TurnOutcome.COMPLETED)

        loop = SubmissionLoop("s", runner, events)
        async with running(loop):
            loop.submit(user())
            await events.wait("approval_requested", turn_id="t")
            loop.submit(Interrupt(session_id="s", operation_id="cancel", turn_id="t"))
            aborted = await events.wait("turn_aborted", turn_id="t")
            self.assertEqual(aborted["result"]["outcome"], "interrupted")
            loop.submit(user("new", "new-start"))
            await events.wait("approval_requested", turn_id="new")
            loop.submit(ApprovalResponse(session_id="s", operation_id="stale", turn_id="t",
                                         request_id="permission-t", approved=True))
            await events.wait("submission_rejected", operation_id="stale")
        self.assertEqual(completed, [])
        self.assertEqual(len([e for e in events.items if e["type"] == "request_cancelled"]), 2)

    async def test_busy_start_and_stale_steer_are_explicitly_rejected(self):
        events = Events()

        async def runner(operation, control):
            await asyncio.Event().wait()

        loop = SubmissionLoop("s", runner, events)
        async with running(loop):
            loop.submit(user())
            await events.wait("turn_started")
            loop.submit(user("second", "busy"))
            loop.submit(Steer(session_id="s", operation_id="wrong-turn", turn_id="second", text="guide"))
            await events.wait("submission_rejected", operation_id="busy")
            await events.wait("submission_rejected", operation_id="wrong-turn")
        self.assertEqual(len([e for e in events.items if e["type"] == "turn_started"]), 1)

    async def test_unconsumed_steer_cannot_be_reported_as_completed(self):
        events = Events()
        release = asyncio.Event()

        async def runner(operation, control):
            await release.wait()
            return TurnResult("s", "t", TurnOutcome.COMPLETED, "old answer")

        loop = SubmissionLoop("s", runner, events)
        async with running(loop):
            loop.submit(user())
            await events.wait("turn_started")
            loop.submit(Steer(session_id="s", operation_id="new-input", turn_id="t", text="New requirement"))
            await events.wait("input_steered")
            release.set()
            event = await events.wait("turn_aborted")
            self.assertIn("unconsumed steering", event["result"]["error"])
            self.assertTrue(any(e["type"] == "input_unconsumed" for e in events.items))
            original = next(e for e in events.items if e.get("operation", {}).get("operation_id") == "new-input")
            self.assertEqual(original["operation"]["text"], "New requirement")

    async def test_runner_error_and_wrong_result_are_recorded_and_session_remains_usable(self):
        events = Events()

        async def runner(operation, control):
            if operation.turn_id == "t":
                raise RuntimeError("fixture failure")
            return TurnResult("another-session", operation.turn_id, TurnOutcome.COMPLETED)

        loop = SubmissionLoop("s", runner, events)
        async with running(loop):
            loop.submit(user())
            first = await events.wait("turn_aborted", turn_id="t")
            self.assertIn("fixture failure", first["result"]["error"])
            loop.submit(user("second", "second"))
            second = await events.wait("turn_aborted", turn_id="second")
            self.assertIn("mismatched", second["result"]["error"])

    async def test_compact_handler_is_injected_and_cannot_receive_regular_steering(self):
        events = Events()
        began = asyncio.Event()

        async def compact(operation, control):
            self.assertIsInstance(operation, Compact)
            began.set()
            await asyncio.Event().wait()

        loop = SubmissionLoop("s", compact, events, compact=compact)
        async with running(loop):
            loop.submit(Compact(session_id="s", operation_id="compact"))
            await asyncio.wait_for(began.wait(), 2)
            loop.submit(Steer(session_id="s", operation_id="guide", turn_id="compact:compact", text="new task"))
            await events.wait("submission_rejected", operation_id="guide")
        self.assertEqual(next(e for e in events.items if e["type"] == "turn_started")["task_kind"], "compact")

    async def test_no_compact_handler_is_an_explicit_rejection(self):
        events = Events()

        async def unused(operation, control):
            self.fail("must not start a task")

        loop = SubmissionLoop("s", unused, events)
        async with running(loop):
            loop.submit(Compact(session_id="s", operation_id="compact"))
            await events.wait("submission_rejected", operation_id="compact")

    async def test_session_attachment_records_real_events_and_closes_resources_after_cancellation(self):
        class UnusedModel:
            def complete(self, *args, **kwargs):
                raise AssertionError("submission does not sample the model")

        with tempfile.TemporaryDirectory() as tmp:
            session = AgentRuntime(build_spec(ROOT, Path(tmp)), UnusedModel()).create_session("inspect")
            controls = []

            async def runner(operation, control):
                controls.append(control)
                await asyncio.Event().wait()

            loop = session.open_submission_loop(runner)
            events = Events(session.emit)
            loop.emit = events
            async with running(loop):
                loop.submit(user(session=session.session_id))
                await events.wait("turn_started")
                with self.assertRaisesRegex(RuntimeError, "Shutdown"):
                    session.close()
                with self.assertRaises(RuntimeError):
                    session.open_submission_loop(runner)
            self.assertTrue(session.closed)
            self.assertTrue(controls[0].closed)
            self.assertTrue(controls[0].cancelled.is_set())
            self.assertEqual(session.events[-1]["type"], "session_shutdown")
            trace = session.recorder.path.read_text()
            self.assertIn('"turn_aborted"', trace)
            self.assertTrue(all(e["session_id"] == session.session_id for e in session.events))

    async def test_shutdown_rejects_queued_operations_and_cancels_before_cleanup(self):
        events = Events()
        order = []

        async def runner(operation, control):
            try:
                await asyncio.Event().wait()
            finally:
                order.append("task stopped")

        loop = SubmissionLoop("s", runner, events, on_shutdown=lambda: order.append("resources closed"))
        async with running(loop) as consumer:
            loop.submit(user())
            await events.wait("turn_started")
            # Ensure the task has actually entered before interrupting it.
            await asyncio.sleep(0)
            loop.submit(Shutdown(session_id="s", operation_id="shutdown"))
            loop.submit(user("never", "queued"))
            await asyncio.wait_for(consumer, 2)
        self.assertEqual(order, ["task stopped", "resources closed"])
        self.assertTrue(any(e.get("operation_id") == "queued" and e["type"] == "submission_rejected" for e in events.items))
        with self.assertRaises(RuntimeError):
            loop.submit(user("closed", "after-close"))

    async def test_external_loop_cancellation_releases_question_waiter(self):
        events = Events()

        async def runner(operation, control):
            await control.request("q", EventKind.USER_INPUT_REQUESTED, "Question?")
            self.fail("cancelled task must not resume")

        loop = SubmissionLoop("s", runner, events)
        consumer = asyncio.create_task(loop.run())
        loop.submit(user())
        await events.wait("user_input_requested")
        consumer.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await consumer
        self.assertFalse(loop.is_running)
        self.assertEqual(events.items[-1]["type"], "session_shutdown")

    async def test_queue_identity_replay_and_close_wakeup(self):
        queue = InputQueue("s")
        queue.submit(user())
        with self.assertRaises(ValueError):
            queue.submit(user())
        with self.assertRaises(ValueError):
            queue.submit(user(session="other"))
        self.assertEqual(await queue.receive(), user())
        waiter = asyncio.create_task(queue.receive())
        queue.close()
        self.assertIsNone(await waiter)

    async def test_completed_turn_rejects_late_steering_and_reused_turn_id(self):
        events = Events()

        async def runner(operation, control):
            return TurnResult("s", "t", TurnOutcome.COMPLETED)

        loop = SubmissionLoop("s", runner, events)
        async with running(loop):
            loop.submit(user())
            await events.wait("turn_completed")
            loop.submit(Steer(session_id="s", operation_id="late", turn_id="t", text="too late"))
            await events.wait("submission_rejected", operation_id="late")
            loop.submit(user(op="reuse-turn"))
            await events.wait("submission_rejected", operation_id="reuse-turn")

    async def test_recording_failure_does_not_enqueue_an_unrecorded_operation(self):
        async def runner(operation, control):
            self.fail("must not execute unrecorded input")

        def broken(kind, **payload):
            raise OSError("fixture recorder failure")

        loop = SubmissionLoop("s", runner, broken)
        with self.assertRaises(OSError):
            loop.submit(user())
        discarded = loop.input_queue.close()
        self.assertEqual(discarded, ())

    async def test_closed_owner_cannot_start_a_previously_attached_consumer(self):
        closed = False
        events = Events()

        async def runner(operation, control):
            self.fail("closed owner must not execute")

        loop = SubmissionLoop("s", runner, events, owner_closed=lambda: closed)
        closed = True
        with self.assertRaises(RuntimeError):
            await loop.run()
        with self.assertRaises(RuntimeError):
            loop.submit(user())

    async def test_approved_request_resumes_once_and_cannot_reuse_request_id(self):
        events = Events()
        replies = []

        async def runner(operation, control):
            reply = await control.request("r", EventKind.APPROVAL_REQUESTED, "Approve?")
            replies.append(reply.approved)
            with self.assertRaises(ValueError):
                await control.request("r", EventKind.APPROVAL_REQUESTED, "Again?")
            return TurnResult("s", "t", TurnOutcome.COMPLETED)

        loop = SubmissionLoop("s", runner, events)
        async with running(loop):
            loop.submit(user())
            await events.wait("approval_requested")
            loop.submit(ApprovalResponse(session_id="s", operation_id="approve", turn_id="t", request_id="r", approved=True))
            await events.wait("turn_completed")
        self.assertEqual(replies, [True])

    async def test_request_registry_rejects_cancelled_reused_and_cross_session_requests(self):
        pending = PendingRequests("s")
        ref = RequestRef("s", "t", "r", EventKind.APPROVAL_REQUESTED)
        future = pending.register(ref)
        wrong = ApprovalResponse(session_id="s", operation_id="reply", turn_id="other", request_id="r", approved=True)
        with self.assertRaises(ValueError):
            pending.resolve(wrong)
        self.assertFalse(future.done())
        pending.cancel_turn("t")
        self.assertTrue(future.cancelled())
        with self.assertRaises(ValueError):
            pending.resolve(replace(wrong, turn_id="t"))
        with self.assertRaises(ValueError):
            pending.register(ref)
        with self.assertRaises(ValueError):
            pending.register(RequestRef("other", "t", "new", EventKind.APPROVAL_REQUESTED))


if __name__ == "__main__":
    unittest.main()
