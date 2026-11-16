import asyncio
import copy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
import tempfile
import unittest
from unittest.mock import patch

from harness.agent import CodingAgent
from harness.core import AgentRuntime
from harness.core.events import EventRecorder
from harness.protocol import Interrupt, Shutdown, Steer, TurnOutcome, TurnResult, UserTurn
from harness.spec import build_spec
from harness.tasks import RegularTask
from harness.core.turn import TurnLoopDriver

ROOT = Path(__file__).resolve().parents[1]


def call(name, arguments, identifier="call"):
    return {"role": "assistant", "content": "", "tool_calls": [{
        "id": identifier, "type": "function", "function": {
            "name": name, "arguments": json.dumps(arguments)}}]}


class ScriptModel:
    def __init__(self, responses):
        self.responses, self.requests = responses, []

    def complete(self, messages, tools, temperature=0):
        self.requests.append(copy.deepcopy(messages))
        response = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return {**copy.deepcopy(response), "_usage": {
            "input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 4,
            "cache_miss_tokens": 3, "requests": 1}}


class BlockingModel(ScriptModel):
    def __init__(self, response):
        super().__init__([response])
        self.loop = asyncio.get_running_loop()
        self.started, self.release = asyncio.Event(), Event()

    def complete(self, messages, tools, temperature=0):
        self.loop.call_soon_threadsafe(self.started.set)
        if not self.release.wait(4):
            raise TimeoutError("fixture model was not released")
        return super().complete(messages, tools, temperature)


def read_only_spec(workspace):
    spec = build_spec(ROOT, Path(workspace))
    return spec


class RegularTaskTest(unittest.IsolatedAsyncioTestCase):
    async def open(self, runtime, text="inspect"):
        session = runtime.create_session(text)
        driver = TurnLoopDriver(runtime.spec, session.model or runtime.model, runtime.execute_tool_calls)
        loop = session.open_submission_loop(RegularTask(session, driver))
        consumer = asyncio.create_task(loop.run())
        return session, loop, consumer

    async def close(self, loop, consumer):
        if not consumer.done():
            loop.submit(Shutdown(session_id=loop.session_id, operation_id="cleanup"))
        await asyncio.wait_for(consumer, 4)

    def submit(self, loop, turn_id="t", text="inspect"):
        loop.submit(UserTurn(session_id=loop.session_id, operation_id="start-" + turn_id,
                             turn_id=turn_id, text=text))

    async def test_actual_model_turns_reuse_session_history_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ScriptModel([{"role": "assistant", "content": "first"},
                                 {"role": "assistant", "content": "second"}])
            session, loop, consumer = await self.open(AgentRuntime(read_only_spec(tmp), model))
            try:
                self.submit(loop)
                first = await asyncio.wait_for(loop.wait_result("t"), 2)
                self.assertEqual(first.last_agent_message, "first")
                self.assertFalse(session.closed)
                self.submit(loop, "next", "Explain the result")
                second = await asyncio.wait_for(loop.wait_result("next"), 2)
                self.assertEqual(second.last_agent_message, "second")
                self.assertIn({"role": "assistant", "content": "first"}, model.requests[1])
                self.assertIn({"role": "user", "content": "Explain the result"}, model.requests[1])
                self.assertEqual(sum(message.get("role") == "user" and "inspect" in str(message.get("content"))
                                     for message in model.requests[0]), 1)
                self.assertEqual(session.usage.input_tokens, 14)
                self.assertEqual(session.usage.cache_read_tokens, 8)
                self.assertEqual(session.task_usage.snapshot()["requests"], 2)
                self.assertEqual({e["turn_id"] for e in session.events if e["type"] == "model"}, {"t", "next"})
            finally:
                await self.close(loop, consumer)
            self.assertTrue(session.closed)

    async def test_pending_guidance_after_legacy_turn_runs_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel({"role": "assistant", "content": "response"})
            session, loop, consumer = await self.open(AgentRuntime(read_only_spec(tmp), model))
            try:
                self.submit(loop)
                await asyncio.wait_for(model.started.wait(), 2)
                loop.submit(Steer(session_id=session.session_id, operation_id="guide", turn_id="t", text="Check alternate explanation"))
                # A queued steer is accepted while the synchronous request runs.
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                model.release.set()
                result = await asyncio.wait_for(loop.wait_result("t"), 2)
                self.assertIs(result.outcome, TurnOutcome.COMPLETED)
                self.assertEqual(len(model.requests), 2)
                self.assertIn({"role": "user", "content": "Check alternate explanation"}, model.requests[1])
                self.assertEqual(len([e for e in session.events if e["type"] == "regular_inputs_consumed"]), 1)
            finally:
                model.release.set()
                await self.close(loop, consumer)

    async def test_cancelled_model_response_cannot_execute_its_tool_and_worker_is_joined(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel(call("load_skill", {"name": "filesystem"}))
            session, loop, consumer = await self.open(AgentRuntime(read_only_spec(tmp), model))
            try:
                self.submit(loop)
                await asyncio.wait_for(model.started.wait(), 2)
                control = loop._control
                loop.submit(Interrupt(session_id=session.session_id, operation_id="cancel", turn_id="t"))
                await asyncio.wait_for(control.cancelled.wait(), 2)
                self.assertFalse(session.closed)
                self.assertTrue(loop._active is not None and not loop._active.done())
                model.release.set()
                result = await asyncio.wait_for(loop.wait_result("t"), 2)
                self.assertIs(result.outcome, TurnOutcome.INTERRUPTED)
                self.assertFalse(any(e["type"] == "tool" for e in session.events))
                self.assertFalse(session.closed)
            finally:
                model.release.set()
                await self.close(loop, consumer)

    async def test_production_bridge_executes_real_read_tool_and_retains_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ScriptModel([
                call("load_skill", {"name": "filesystem"}, "load"),
                call("read_file", {"path": "sample.txt"}, "read"),
                {"role": "assistant", "content": "Read complete"},
            ])
            runtime = AgentRuntime(read_only_spec(tmp), model)
            session, loop, consumer = await self.open(runtime)
            session.tools.context.executor.write_file(Path(tmp) / "sample.txt", b"fixture evidence")
            try:
                self.submit(loop)
                result = await asyncio.wait_for(loop.wait_result("t"), 2)
                self.assertEqual(result.last_agent_message, "Read complete")
                read = next(e for e in session.events if e.get("name") == "read_file" and e["type"] == "tool")
                self.assertTrue(read["ok"])
                self.assertIn("fixture evidence", read["output"])
                self.assertTrue(any(m.get("tool_call_id") == "read" for m in model.requests[-1]))
            finally:
                await self.close(loop, consumer)

    async def test_error_with_pending_input_does_not_restart_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            class FailingBlock(BlockingModel):
                def complete(self, messages, tools, temperature=0):
                    super().complete(messages, tools, temperature)
                    raise RuntimeError("fixture provider failed")

            model = FailingBlock({"role": "assistant", "content": "unused"})
            session, loop, consumer = await self.open(AgentRuntime(read_only_spec(tmp), model))
            try:
                self.submit(loop)
                await asyncio.wait_for(model.started.wait(), 2)
                loop.submit(Steer(session_id=session.session_id, operation_id="guide", turn_id="t", text="new input"))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                model.release.set()
                result = await asyncio.wait_for(loop.wait_result("t"), 2)
                self.assertIs(result.outcome, TurnOutcome.ERROR)
                self.assertIn("fixture provider failed", result.error)
                self.assertEqual(len(model.requests), 1)
                self.assertTrue(any(e["type"] == "input_unconsumed" for e in session.events))
                self.assertEqual([item.text for item in session.deferred_inputs], ["new input"])
                self.assertFalse(session.closed)
            finally:
                model.release.set()
                await self.close(loop, consumer)

    async def test_explicit_step_budget_cannot_reset_on_pending_input_continuation(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel({"role": "assistant", "content": "response"})
            spec = read_only_spec(tmp)
            spec = replace(spec, orchestration=replace(spec.orchestration, max_turns=1))
            session, loop, consumer = await self.open(AgentRuntime(spec, model))
            try:
                self.submit(loop)
                await asyncio.wait_for(model.started.wait(), 2)
                loop.submit(Steer(session_id=session.session_id, operation_id="guide", turn_id="t", text="continue"))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                model.release.set()
                result = await asyncio.wait_for(loop.wait_result("t"), 2)
                self.assertIs(result.outcome, TurnOutcome.BUDGET_LIMITED)
                self.assertEqual(len(model.requests), 1)
            finally:
                model.release.set()
                await self.close(loop, consumer)

    async def test_opt_in_one_shot_preserves_result_shape_and_trace(self):
        with tempfile.TemporaryDirectory() as tmp:
            agent = CodingAgent(read_only_spec(tmp), ScriptModel([{"role": "assistant", "content": "done"}]))
            result = await agent.run_async("inspect")
            self.assertEqual(result.text, "done")
            self.assertFalse(result.verified)
            self.assertIn("verification not evaluated", result.reasons)
            self.assertEqual(result.events[-1]["type"], "run_end")
            self.assertTrue(any(e["type"] == "session_shutdown" for e in result.events))
            self.assertEqual(result.events[-1]["task_usage"]["requests"], 1)

    async def test_opt_in_one_shot_error_is_recorded_without_another_model_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ScriptModel([RuntimeError("fixture failure")])
            result = await CodingAgent(read_only_spec(tmp), model).run_async("inspect")
            self.assertFalse(result.verified)
            self.assertEqual(result.events[-1]["termination"], "error")
            self.assertIn("fixture failure", ";".join(result.reasons))
            self.assertEqual(len(model.requests), 1)

    async def test_result_waiters_wake_for_busy_turn_and_shutdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel({"role": "assistant", "content": "done"})
            session, loop, consumer = await self.open(AgentRuntime(read_only_spec(tmp), model))
            try:
                self.submit(loop)
                await asyncio.wait_for(model.started.wait(), 2)
                self.submit(loop, "busy")
                with self.assertRaisesRegex(ValueError, "busy"):
                    await asyncio.wait_for(loop.wait_result("busy"), 2)
                with self.assertRaises(ValueError):
                    await loop.wait_result("unknown")
            finally:
                model.release.set()
                await self.close(loop, consumer)

    async def test_recorder_keeps_ids_and_disk_order_under_concurrent_submissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            recorder = EventRecorder(Path(tmp) / "trace.jsonl")
            recorder.initialize()
            with ThreadPoolExecutor(max_workers=4) as workers:
                list(workers.map(lambda i: recorder.emit("fixture", index=i), range(100)))
            persisted = [json.loads(line) for line in recorder.path.read_text().splitlines()]
            self.assertEqual([e["event_id"] for e in recorder.events], [e["event_id"] for e in persisted])
            self.assertEqual(len(set(e["event_id"] for e in persisted)), 100)
            self.assertEqual({e["index"] for e in persisted}, set(range(100)))

    async def test_cancelled_one_shot_joins_worker_before_close_even_on_repeated_cancel(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel(call("load_skill", {"name": "filesystem"}))
            runtime = AgentRuntime(read_only_spec(tmp), model)
            session = runtime.create_session("inspect")
            from harness.tasks.oneshot import run_once
            with patch.object(runtime, "create_session", return_value=session):
                task = asyncio.create_task(run_once(runtime, "inspect"))
                try:
                    await asyncio.wait_for(model.started.wait(), 2)
                    control = session.submission_loop._control
                    task.cancel()
                    await asyncio.wait_for(control.cancelled.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertFalse(session.closed)
                    model.release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
                    self.assertTrue(session.closed)
                    self.assertFalse(any(e["type"] == "tool" for e in session.events))
                    self.assertEqual(session.events[-1]["type"], "run_end")
                finally:
                    model.release.set()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_new_user_turn_after_interrupt_can_still_use_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = BlockingModel({"role": "assistant", "content": "done"})
            session, loop, consumer = await self.open(AgentRuntime(read_only_spec(tmp), model))
            try:
                self.submit(loop)
                await asyncio.wait_for(model.started.wait(), 2)
                control = loop._control
                loop.submit(Interrupt(session_id=session.session_id, operation_id="cancel", turn_id="t"))
                await asyncio.wait_for(control.cancelled.wait(), 2)
                model.release.set()
                self.assertIs((await loop.wait_result("t")).outcome, TurnOutcome.INTERRUPTED)
                self.submit(loop, "next", "continue inspection")
                result = await asyncio.wait_for(loop.wait_result("next"), 2)
                self.assertIs(result.outcome, TurnOutcome.COMPLETED)
                self.assertEqual(session.state.status, "idle")
            finally:
                model.release.set()
                await self.close(loop, consumer)

    async def test_completion_evidence_still_rejects_unverified_final_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_spec(ROOT, Path(tmp))

            model = ScriptModel([{"role": "assistant", "content": "Could not finish"}])
            result = await CodingAgent(spec, model).run_async("inspect")
            self.assertFalse(result.verified)
            self.assertEqual(len(model.requests), 1)
            self.assertEqual(result.events[-1]["verification_status"], "not_evaluated")

    async def test_shutdown_wakes_waiter_for_queued_unstarted_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = ScriptModel([{"role": "assistant", "content": "unused"}])
            session, loop, consumer = await self.open(AgentRuntime(read_only_spec(tmp), model))
            loop.submit(Shutdown(session_id=session.session_id, operation_id="stop-first"))
            self.submit(loop, "never")
            waiter = asyncio.create_task(loop.wait_result("never"))
            with self.assertRaisesRegex(RuntimeError, "shut down"):
                await asyncio.wait_for(waiter, 2)
            await consumer
            self.assertTrue(session.closed)
            self.assertEqual(model.requests, [])


if __name__ == "__main__":
    unittest.main()
