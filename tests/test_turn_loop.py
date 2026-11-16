"""Offline integration checks for the opt-in, final-response turn loop."""
import asyncio
import copy
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
import tempfile
import unittest

from harness.agent import CodingAgent
from harness.core import AgentRuntime
from harness.core.assembly import AssemblyProfile
from harness.core.sampling import SamplingResult
from harness.core.turn import TurnLoopDriver
from harness.media.images import ModelImageInputError, has_images
from harness.model_limits import ModelContextLengthError, ModelPricing
from harness.protocol import Interrupt, Shutdown, Steer, TurnOutcome, UserTurn
from harness.spec import build_spec
from harness.tasks import RegularTask
from tests.test_native_images import png

ROOT = Path(__file__).resolve().parents[1]


def call(name, arguments, identifier="call"):
    return {"id": identifier, "type": "function", "function": {
        "name": name, "arguments": json.dumps(arguments)}}


def response(*calls, text="", **extra):
    return {"role": "assistant", "content": text, **({"tool_calls": list(calls)} if calls else {}), **extra}


class Model:
    supports_image_input = True
    pricing = ModelPricing(1, 2, 0.1)

    def __init__(self, responses):
        self.responses, self.requests = responses, []

    def complete(self, messages, tools, temperature=0):
        self.requests.append(copy.deepcopy(messages))
        item = self.responses[min(len(self.requests) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return {**copy.deepcopy(item), "_usage": {
            "input_tokens": 7, "output_tokens": 3, "cache_read_tokens": 4,
            "cache_miss_tokens": 3, "requests": 1}}


class TurnLoopTest(unittest.IsolatedAsyncioTestCase):
    async def open(self, tmp, model, *, spec=None, execute=None):
        runtime = AgentRuntime(spec or build_spec(ROOT, Path(tmp)), model)
        session = runtime.create_session("inspect")
        driver = TurnLoopDriver(runtime.spec, model, execute or runtime.execute_tool_calls)
        loop = session.open_submission_loop(RegularTask(session, driver))
        consumer = asyncio.create_task(loop.run())
        self.addAsyncCleanup(self.close, loop, consumer)
        return session, loop

    async def close(self, loop, consumer):
        if not consumer.done():
            loop.submit(Shutdown(session_id=loop.session_id, operation_id="cleanup"))
        await asyncio.wait_for(consumer, 5)

    def submit(self, loop, turn="t"):
        loop.submit(UserTurn(session_id=loop.session_id, operation_id="start-" + turn,
                             turn_id=turn, text="inspect"))

    async def result(self, loop, turn="t"):
        return await asyncio.wait_for(loop.wait_result(turn), 4)

    async def test_honest_failure_is_final_not_a_completion_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([response(text="I cannot solve this task.")])
            result = await CodingAgent(build_spec(ROOT, Path(tmp)), model).run_async("inspect")
            self.assertEqual(len(model.requests), 1)
            self.assertFalse(result.verified)
            self.assertIn("verification not evaluated", result.reasons)
            self.assertFalse(any("completion_attempt" in event["type"] for event in result.events))

    async def test_persistence_guidance_reaches_request_without_forced_followup(self):
        prompt = (ROOT / "harness/prompts/system.md").read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([response(text="Here is the requested proposal; no changes made.")])
            result = await CodingAgent(build_spec(ROOT, Path(tmp)), model).run_async(
                "Explain a possible fix. Do not implement it.")
            self.assertEqual(len(model.requests), 1)
            systems = "\n".join(m["content"] for m in model.requests[0] if m["role"] == "system")
            self.assertIn(prompt, systems)
            for section in ("Execution responsibility", "When to continue",
                            "Evidence and verification", "Productive persistence", "When to return"):
                self.assertIn(section, systems)
            self.assertFalse(any(e["type"] in ("tool_started", "completion_attempt")
                                 for e in result.events))
            self.assertFalse(result.verified)

    async def test_failed_tool_still_allows_final_unresolved_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([
                response(call("read_file", {"path": "missing-input.txt"})),
                response(text="Required input is missing. Please provide missing-input.txt."),
            ])
            result = await CodingAgent(build_spec(ROOT, Path(tmp)), model).run_async(
                "Analyze the supplied missing-input.txt; do not invent its contents.")
            self.assertEqual(len(model.requests), 2)
            self.assertTrue(any(e["type"] == "tool_failed" for e in result.events))
            self.assertFalse(any("completion_attempt" in e["type"] for e in result.events))
            self.assertIn("Required input is missing", result.text)

    async def test_turn_assembly_loads_configured_system_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_spec(ROOT, Path(tmp))
            custom = Path(tmp) / "custom-system.md"
            custom.write_text("Configured system prompt sentinel", encoding="utf-8")
            spec = replace(spec, prompt_path=custom)
            runtime = AgentRuntime(spec, Model([response(text="unused")]))
            session = runtime.create_session("inspect", profile=AssemblyProfile.TURN)
            try:
                system = session.messages[0]["content"]
                self.assertTrue(system.startswith(spec.prompt_path.read_text(encoding="utf-8")))
                self.assertIn("Configured system prompt sentinel", system)
                self.assertEqual(runtime.model.requests, [])
            finally:
                session.close()

    async def test_real_tools_reasoning_raw_audit_and_usage(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([
                response(call("read_file", {"path": "sample.txt"}, "a"),
                         call("list_directory", {"path": "."}, "b"), reasoning_content="preserved reasoning"),
                response(text="done"),
            ])
            session, loop = await self.open(tmp, model)
            session.tools.enable_skill("filesystem")
            session.tools.context.executor.write_file(Path(tmp) / "sample.txt", b"evidence")
            self.submit(loop)
            result = await self.result(loop)
            self.assertIs(result.outcome, TurnOutcome.COMPLETED)
            messages = model.requests[1]
            index = next(i for i, m in enumerate(messages) if m.get("tool_calls"))
            self.assertEqual(messages[index]["reasoning_content"], "preserved reasoning")
            self.assertEqual([m.get("tool_call_id") for m in messages[index+1:index+3]], ["a", "b"])
            self.assertEqual(session.usage.input_tokens, 14)
            self.assertEqual(session.usage.cache_read_tokens, 8)
            self.assertAlmostEqual(session.task_usage.snapshot()["cost_usd"], 0.0000188)
            raw = [e for e in session.events if e["type"] == "sampling_response"]
            self.assertEqual(raw[0]["response"]["reasoning_content"], "preserved reasoning")
            self.assertEqual([e["sampling_index"] for e in raw], [1, 2])
            self.submit(loop, "next")
            await self.result(loop, "next")
            self.assertEqual([e["sampling_index"] for e in session.events if e["type"] == "sampling_response"], [1, 2, 1])

    async def test_truncated_tool_calls_are_audited_but_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([response(call("load_skill", {"name": "filesystem"}), _finish_reason="length"),
                           response(text="unable to proceed")])
            session, loop = await self.open(tmp, model)
            self.submit(loop)
            self.assertIs((await self.result(loop)).outcome, TurnOutcome.COMPLETED)
            self.assertEqual(len(model.requests), 2)
            self.assertFalse(any(e["type"] == "tool" for e in session.events))
            self.assertFalse(any(m.get("tool_calls") for m in session.messages))
            self.assertTrue(any(e["type"] == "model_response_retry" and e["observation_ref"] for e in session.events))
            self.assertEqual(session.usage.requests, 2)

    async def test_repeated_empty_responses_have_error_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([response()])
            session, loop = await self.open(tmp, model)
            self.submit(loop)
            self.assertIs((await self.result(loop)).outcome, TurnOutcome.ERROR)
            self.assertEqual(len(model.requests), 3)
            self.assertEqual(len([e for e in session.events if e["type"] == "sampling_response"]), 3)

    async def test_provider_context_rejection_has_bounded_exit(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([ModelContextLengthError("maximum context length is 100 requested 10000")])
            session, loop = await self.open(tmp, model)
            self.submit(loop)
            self.assertIs((await self.result(loop)).outcome, TurnOutcome.ERROR)
            self.assertLessEqual(len(model.requests), 4)
            self.assertFalse(session.closed)

    async def test_provider_context_recovery_retains_raw_history(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([ModelContextLengthError("context limit: 10000 requested 11000"),
                           response(text="recovered")])
            session, loop = await self.open(tmp, model)
            history = [{"role": "assistant", "content": f"observation {i}: " + "data " * 1000}
                       for i in range(8)]
            session.messages.extend(history)
            self.submit(loop)
            self.assertIs((await self.result(loop)).outcome, TurnOutcome.COMPLETED)
            self.assertEqual(len(model.requests), 2)
            self.assertTrue(all(message in session.messages for message in history))
            self.assertEqual([e["sampling_index"] for e in session.events if e["type"] == "model_requested"], [1, 2])
            self.assertTrue(any(e["type"] == "context_compaction" for e in session.events))

    async def test_explicit_cost_budget_prevents_next_sampling(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_spec(ROOT, Path(tmp))
            spec = replace(spec, budget=replace(spec.budget, max_cost_usd=0.000001))
            model = Model([response(call("load_skill", {"name": "filesystem"}))])
            session, loop = await self.open(tmp, model, spec=spec)
            self.submit(loop)
            self.assertIs((await self.result(loop)).outcome, TurnOutcome.BUDGET_LIMITED)
            self.assertEqual(len(model.requests), 1)
            self.assertEqual(session.task_usage.snapshot()["requests"], 1)

    async def test_cancel_signal_reaches_cooperative_tool_wait(self):
        with tempfile.TemporaryDirectory() as tmp:
            started = asyncio.Event()
            event_loop = asyncio.get_running_loop()

            def waiting(session, batch, turn, cancelled):
                event_loop.call_soon_threadsafe(started.set)
                # Wait in the worker, as process/background wait tools do.
                deadline = Event()
                for _ in range(100):
                    if session.tools.context.interrupt_wait():
                        return
                    deadline.wait(0.01)
                raise TimeoutError("tool did not receive cancellation")

            model = Model([response(call("wait_for_tasks", {}))])
            session, loop = await self.open(tmp, model, execute=waiting)
            self.submit(loop)
            await asyncio.wait_for(started.wait(), 2)
            loop.submit(Interrupt(session_id=session.session_id, operation_id="cancel", turn_id="t"))
            self.assertIs((await self.result(loop)).outcome, TurnOutcome.INTERRUPTED)

    async def test_native_images_reach_model_and_rejection_falls_back(self):
        for reject in (False, True):
            with self.subTest(reject=reject), tempfile.TemporaryDirectory() as tmp:
                items = [response(call("view_image", {"path": "sample.png"}))]
                if reject:
                    items.append(ModelImageInputError("image input unsupported"))
                items.append(response(text="inspected"))
                model = Model(items)
                session, loop = await self.open(tmp, model)
                session.tools.enable_skill("filesystem")
                session.tools.context.executor.write_file(Path(tmp) / "sample.png", png())
                self.submit(loop)
                self.assertIs((await self.result(loop)).outcome, TurnOutcome.COMPLETED)
                self.assertTrue(any(has_images(m) for m in model.requests[1]))
                if reject:
                    self.assertFalse(any(has_images(m) for m in model.requests[2]))


    async def test_unsupported_output_item_is_fed_back_to_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([
                response(text="provider emitted an extension", _unsupported_output_items=["future_provider_item"]),
                response(text="used only supported output"),
            ])
            session, loop = await self.open(tmp, model)
            self.submit(loop)
            result = await self.result(loop)
            self.assertIs(result.outcome, TurnOutcome.COMPLETED)
            self.assertEqual(result.last_agent_message, "used only supported output")
            self.assertEqual(len(model.requests), 2)
            notice = model.requests[1][-1]["content"]
            self.assertIn("Harness protocol notice", notice)
            self.assertIn("future_provider_item", notice)
            self.assertTrue(any(e["type"] == "model_output_unsupported" for e in session.events))

    async def test_duplicate_tool_ids_fail_before_execution(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([response(call("load_skill", {"name": "filesystem"}),
                                    call("load_skill", {"name": "shell"}))])
            session, loop = await self.open(tmp, model)
            self.submit(loop)
            result = await self.result(loop)
            self.assertIs(result.outcome, TurnOutcome.ERROR)
            self.assertIn("unique nonempty IDs", result.error)
            self.assertFalse(any(e["type"] == "tool" for e in session.events))
            self.assertTrue(any(e["type"] == "sampling_response" for e in session.events))

    async def test_unknown_tool_effect_is_not_automatically_replayed(self):
        with tempfile.TemporaryDirectory() as tmp:
            calls = []

            def broken(session, batch, turn, cancelled):
                calls.append(batch)
                session.tools.context.executor.write_file(Path(tmp) / "effect.txt", b"effect")
                raise RuntimeError("result delivery failed after effect")

            model = Model([response(call("write_file", {"path": "effect.txt", "content": "effect"})),
                           response(text="report error")])
            session, loop = await self.open(tmp, model, execute=broken)
            self.submit(loop)
            self.assertIs((await self.result(loop)).outcome, TurnOutcome.ERROR)
            output = json.loads(next(m["content"] for m in session.messages if m.get("role") == "tool"))
            self.assertEqual(output["execution_status"], "unknown")
            self.assertFalse(output["retry_automatically"])
            self.submit(loop, "next")
            self.assertIs((await self.result(loop, "next")).outcome, TurnOutcome.COMPLETED)
            self.assertEqual(len(calls), 1)

    async def test_explicit_tool_budget_closes_whole_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([response(call("list_directory", {"path": "."}, "a"),
                                    call("list_directory", {"path": "."}, "b"))])
            session, loop = await self.open(tmp, model)
            session.tools.enable_skill("filesystem")
            session.tool_call_budget = 1
            self.submit(loop)
            self.assertIs((await self.result(loop)).outcome, TurnOutcome.BUDGET_LIMITED)
            outputs = [m for m in session.messages if m.get("role") == "tool"]
            self.assertEqual([m["tool_call_id"] for m in outputs], ["a", "b"])
            self.assertFalse(json.loads(outputs[1]["content"])["executed"])
            self.assertEqual(len([e for e in session.events if e["type"] == "tool"]), 1)
            self.assertEqual(len(model.requests), 1)

    async def test_budget_on_new_turn_does_not_return_stale_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            model = Model([response(text="old answer")])
            session, loop = await self.open(tmp, model)
            self.submit(loop)
            await self.result(loop)
            session.tool_call_budget = 0
            self.submit(loop, "next")
            result = await self.result(loop, "next")
            self.assertIs(result.outcome, TurnOutcome.BUDGET_LIMITED)
            self.assertIsNone(result.last_agent_message)

    async def test_live_steering_continues_after_final_response(self):
        with tempfile.TemporaryDirectory() as tmp:
            started, release = asyncio.Event(), Event()
            event_loop = asyncio.get_running_loop()
            model = Model([response(text="first"), response(text="adjusted")])
            original = model.complete

            def complete(*args):
                if not model.requests:
                    event_loop.call_soon_threadsafe(started.set)
                    if not release.wait(4):
                        raise TimeoutError("test did not release model")
                return original(*args)

            model.complete = complete
            session, loop = await self.open(tmp, model)
            self.submit(loop)
            try:
                await asyncio.wait_for(started.wait(), 2)
                loop.submit(Steer(session_id=session.session_id, operation_id="steer", turn_id="t", text="new evidence"))
                await asyncio.sleep(0)
                await asyncio.sleep(0)
            finally:
                release.set()
            result = await self.result(loop)
            self.assertEqual(result.last_agent_message, "adjusted")
            self.assertIn({"role": "user", "content": "new evidence"}, model.requests[1])

    async def test_cancelled_batch_closes_unexecuted_calls_before_reuse(self):
        with tempfile.TemporaryDirectory() as tmp:
            started, release = asyncio.Event(), Event()
            event_loop = asyncio.get_running_loop()
            model = Model([response(call("list_directory", {"path": "."}, "a"),
                                    call("list_directory", {"path": "."}, "b")), response(text="next")])
            session, loop = await self.open(tmp, model)
            session.tools.enable_skill("filesystem")
            original = session.tools.dispatch

            def execute(*args, **kwargs):
                if args[0].name == "list_directory":
                    event_loop.call_soon_threadsafe(started.set)
                    if not release.wait(4):
                        raise TimeoutError("test did not release tool")
                return original(*args, **kwargs)

            session.tools.dispatch = execute
            self.submit(loop)
            try:
                await asyncio.wait_for(started.wait(), 2)
                control = loop._control
                loop.submit(Interrupt(session_id=session.session_id, operation_id="cancel", turn_id="t"))
                await asyncio.wait_for(control.cancelled.wait(), 2)
            finally:
                release.set()
            self.assertIs((await self.result(loop)).outcome, TurnOutcome.INTERRUPTED)
            outputs = [m for m in session.messages if m.get("role") == "tool"]
            self.assertEqual([m["tool_call_id"] for m in outputs], ["a", "b"])
            self.assertEqual(json.loads(outputs[1]["content"])["failure_kind"], "cancelled")
            self.assertFalse(json.loads(outputs[1]["content"])["executed"])
            self.submit(loop, "next")
            self.assertIs((await self.result(loop, "next")).outcome, TurnOutcome.COMPLETED)


class SamplingResultTest(unittest.TestCase):
    def test_exactly_one_variant_required(self):
        for values in ({}, {"response": {}, "retry": True}, {"retry": True, "termination": "error"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                SamplingResult(**values)
