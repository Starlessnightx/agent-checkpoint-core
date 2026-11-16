from __future__ import annotations

import io
import asyncio
import json
import os
import tempfile
import unittest
import urllib.error
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from harness.context.manager import ContextManager, estimate_tokens
from harness.core import AgentRuntime
from harness.core.state import TaskUsageLedger
from harness.model import OpenAICompatibleModel
from harness.model_limits import ModelContextLengthError, ModelLimits, ModelPricing
from harness.spec import BudgetSpec, build_spec
from harness.subagents import AgentSpec
from harness.subagents.coordinator import SubagentCoordinator
from harness.tasks.delegated import run_delegated
from harness.tasks.oneshot import _run_session_once


ROOT = Path(__file__).resolve().parents[1]


class _CountingModel:
    def __init__(self, input_tokens: int = 0):
        self.calls = 0
        self.input_tokens = input_tokens

    def complete(self, messages, tools, temperature=0):
        self.calls += 1
        return {
            "role": "assistant", "content": "Done.",
            "_usage": {
                "input_tokens": self.input_tokens,
                "output_tokens": 1, "requests": 1,
            },
        }


class _ContinuingUsageModel(_CountingModel):
    def complete(self, messages, tools, temperature=0):
        response = super().complete(messages, tools, temperature)
        response["content"] = ""
        response["tool_calls"] = [{"id": f"usage-{self.calls}", "type": "function",
            "function": {"name": "list_skills", "arguments": "{}"}}]
        return response


class _TruncatedToolModel:
    def __init__(self):
        self.calls = 0

    def complete(self, messages, tools, temperature=0):
        self.calls += 1
        if self.calls == 1:
            return {
                "role": "assistant", "content": "",
                "_finish_reason": "length",
                "tool_calls": [{
                    "id": "partial", "type": "function",
                    "function": {
                        "name": "write_file",
                        "arguments": json.dumps({
                            "path": "should-not-exist.txt", "content": "unsafe"
                        }),
                    },
                }],
            }
        return {"role": "assistant", "content": "Cannot finish yet."}


class _ContextRetryModel:
    def __init__(self):
        self.requests = []

    def complete(self, messages, tools, temperature=0):
        self.requests.append(messages)
        if len(self.requests) == 1:
            raise ModelContextLengthError("context length exceeded")
        return {"role": "assistant", "content": "Recovered."}


class _ChildUsageModel:
    def complete(self, messages, tools, temperature=0):
        return {
            "role": "assistant", "content": "",
            "tool_calls": [{
                "id": "child-call", "type": "function",
                "function": {"name": "list_skills", "arguments": "{}"},
            }],
            "_usage": {"input_tokens": 20, "output_tokens": 2, "requests": 1},
        }


class _LargeChildUsageModel:
    def complete(self, messages, tools, temperature=0):
        return {
            "role": "assistant", "content": "Inspection complete.",
            "_usage": {"input_tokens": 100_000, "output_tokens": 10,
                       "requests": 1},
        }


class RuntimeBudgetTest(unittest.TestCase):
    def test_default_has_no_cumulative_or_turn_token_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_spec(ROOT, Path(tmp))
            model = _ContinuingUsageModel(input_tokens=400_000)
            runtime = AgentRuntime(spec, model)
            session = runtime.create_session("Produce an artifact")
            try:
                result = asyncio.run(run_delegated(runtime, session, lambda: {"cancel": model.calls >= 4}))
            finally:
                session.close()
            self.assertEqual(model.calls, 4)
            self.assertNotIn("budget_exhausted", [
                event.get("termination") for event in result.events
            ])
            self.assertEqual(result.events[-1]["termination"], "interrupted")
            self.assertEqual(result.events[-1]["task_usage"]["input_tokens"], 1_600_000)

    def test_explicit_cumulative_budget_still_stops_next_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = replace(
                build_spec(ROOT, Path(tmp)),
                budget=BudgetSpec(max_cumulative_input_tokens=10),
            )
            model = _ContinuingUsageModel(input_tokens=12)
            result = AgentRuntime(spec, model).run("Produce an artifact")
            self.assertEqual(model.calls, 1)
            self.assertEqual(result.events[-1]["termination"], "budget_limited")

    def test_task_usage_ledger_aggregates_cost_and_cached_input(self):
        ledger = TaskUsageLedger()
        pricing = ModelPricing(2.0, 10.0, 0.5)
        first = {"input_tokens": 100, "cache_read_tokens": 80,
                 "output_tokens": 10, "requests": 1}
        second = {"input_tokens": 50, "cache_read_tokens": 0,
                  "output_tokens": 5, "requests": 1}
        ledger.add(first, cost_usd=pricing.request_cost(first))
        ledger.add(second, cost_usd=pricing.request_cost(second))
        self.assertEqual(ledger.snapshot()["input_tokens"], 150)
        self.assertEqual(ledger.snapshot()["cache_read_tokens"], 80)
        self.assertAlmostEqual(ledger.snapshot()["cost_usd"], 0.00033)

    def test_parent_task_usage_includes_child_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            runtime = AgentRuntime(
                build_spec(ROOT, Path(tmp)), _CountingModel(),
                model_resolver=lambda _: _ChildUsageModel(),
            )
            parent = runtime.create_session("parent task")
            self.addCleanup(parent.close)
            agent_id = parent.agent_manager.spawn(
                AgentSpec(
                    name="explorer", model="child-model",
                    capabilities=("list_skills",), max_turns=1,
                    max_tool_calls=2,
                ),
                "Inspect the available skills",
            )
            child = parent.subagents.collect(agent_id, -1)
            self.assertIsNotNone(child)
            self.assertEqual(parent.task_usage.snapshot()["input_tokens"], 20)
            self.assertEqual(parent.task_usage.snapshot()["output_tokens"], 2)

    def test_default_child_role_has_no_cumulative_token_cutoff(self):
        with tempfile.TemporaryDirectory() as tmp:
            coordinator = SubagentCoordinator(
                _LargeChildUsageModel(), Path(tmp), max_parallel=1
            )
            try:
                agent_id = coordinator.spawn("explorer", "Inspect the project", [])
                child = coordinator.collect(agent_id, -1)
                self.assertIsNotNone(child)
                assert child is not None
                self.assertEqual(child.status, "completed")
                self.assertEqual(child.usage["input_tokens"], 100_000)
            finally:
                coordinator.close()

    def test_cost_cap_requires_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = replace(
                build_spec(ROOT, Path(tmp)), budget=BudgetSpec(max_cost_usd=1.0)
            )
            with self.assertRaisesRegex(ValueError, "verified prices"):
                AgentRuntime(spec, _CountingModel()).create_session("task")

    def test_cost_cap_stops_after_accounted_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = replace(
                build_spec(ROOT, Path(tmp)), budget=BudgetSpec(max_cost_usd=5.0)
            )
            model = _ContinuingUsageModel(input_tokens=12)
            model.pricing = ModelPricing(1_000_000, 1_000_000)
            result = AgentRuntime(spec, model).run("Produce an artifact")
            self.assertEqual(model.calls, 1)
            self.assertEqual(result.events[-1]["termination"], "budget_limited")
            self.assertEqual(result.events[-1]["task_usage"]["cost_usd"], 13.0)

    def test_model_capacity_and_budget_are_explicitly_configurable(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "MODEL_API_KEY": "local-test-key",
            "MODEL_CONTEXT_WINDOW_TOKENS": "1000",
            "MODEL_MAX_OUTPUT_TOKENS": "250",
            "HARNESS_MAX_CUMULATIVE_INPUT_TOKENS": "5000",
            "HARNESS_MAX_TURNS": "7",
        }):
            spec = build_spec(ROOT, Path(tmp))
            model = OpenAICompatibleModel.from_config(spec)
            self.assertEqual(model.limits.input_capacity, 750)
            self.assertEqual(spec.budget.max_cumulative_input_tokens, 5000)
            self.assertEqual(spec.orchestration.max_turns, 7)

    def test_model_limits_reserve_output_space(self):
        self.assertEqual(ModelLimits(1000, 250).input_capacity, 750)
        self.assertEqual(ModelLimits(1000, None).input_capacity, 900)
        self.assertEqual(
            ModelLimits(1000, None, output_reserve_tokens=200).input_capacity,
            800,
        )
        self.assertEqual(
            ModelLimits(1000, None, input_capacity_tokens=700).input_capacity,
            700,
        )
        with self.assertRaises(ValueError):
            ModelLimits(1000, 1000)

    def test_tool_schemas_are_counted_before_request(self):
        policy = {"max_messages": 100, "keep_first": 2, "keep_latest": 12,
                  "compact_at_ratio": 1000}
        messages = [
            {"role": "system", "content": "contract"},
            {"role": "user", "content": "task"},
            *[{"role": "assistant", "content": str(index) + "x" * 500}
              for index in range(4)],
        ]
        schemas = [{"type": "function", "function": {
            "name": "large_tool", "description": "z" * 800,
        }}]
        with tempfile.TemporaryDirectory() as tmp:
            first = ContextManager(policy, Path(tmp), "task", max_context_tokens=None)
            original, _ = first.build_model_context(messages, [], schemas)
            full_request = estimate_tokens(
                original.messages + [{"role": "tool_schemas", "content": schemas}]
            )
            fitted = ContextManager(
                policy, Path(tmp), "task", max_context_tokens=full_request - 100
            )
            result, event = fitted.build_model_context(messages, [], schemas)
            self.assertIsNotNone(event)
            assert event is not None
            self.assertEqual(event["reason"], "request_capacity")
            self.assertLessEqual(fitted.last_metrics["request_tokens"], full_request - 100)
            self.assertEqual(result.messages[-1], messages[-1])
            self.assertFalse(any(str(m.get("content", "")).startswith("Structured model view") for m in result.messages))
            self.assertTrue(Path(tmp, event["archive"]).is_file())

    def test_truncated_tool_call_is_not_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            model = _TruncatedToolModel()
            spec = build_spec(ROOT, workspace)
            spec = replace(spec, orchestration=replace(spec.orchestration, max_turns=3))
            result = AgentRuntime(spec, model).run(
                "Create an artifact"
            )
            self.assertFalse((workspace / "should-not-exist.txt").exists())
            self.assertTrue(any(
                event.get("type") == "model_response_retry"
                and event.get("discarded_tool_calls") == 1
                for event in result.events
            ))

    def test_provider_context_rejection_compacts_and_retries(self):
        with tempfile.TemporaryDirectory() as tmp:
            spec = build_spec(ROOT, Path(tmp))
            model = _ContextRetryModel()
            runtime = AgentRuntime(spec, model)
            session = runtime.create_session("task")
            self.addCleanup(session.close)
            session.messages.extend(
                {"role": "assistant", "content": f"old observation {index}"}
                for index in range(4)
            )
            result = asyncio.run(_run_session_once(runtime, session, "task"))
            self.assertEqual(len(model.requests), 2)
            self.assertLess(len(model.requests[1]), len(model.requests[0]))
            self.assertTrue(any(
                event.get("type") == "context_compaction"
                and event.get("reason") == ["provider_context_limit"]
                for event in result.events
            ))

    @patch("harness.model.urllib.request.urlopen")
    def test_provider_http_context_error_is_classified_without_retry(self, urlopen):
        urlopen.side_effect = urllib.error.HTTPError(
            "https://example.test/chat/completions", 400, "bad request", {},
            io.BytesIO(b'{"error":"context_length_exceeded"}'),
        )
        model = OpenAICompatibleModel(
            "https://example.test", "test-key", "test-model"
        )
        with self.assertRaises(ModelContextLengthError):
            model.complete([], [])
        self.assertEqual(urlopen.call_count, 1)


if __name__ == "__main__":
    unittest.main()
