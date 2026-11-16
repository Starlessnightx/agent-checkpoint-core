import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from harness.core.tool_execution import _prefetch_parallel_results
from harness.core import AgentRuntime
from harness.core.hooks import HookPipeline
from harness.lsp.tools import lsp_tool_specs
from harness.tools.registry import ToolRegistry, default_tool_specs
from harness.trusted.deployment import ContainerFullAccessPolicy


class ToolCompatibilityTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(); self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.registry = ToolRegistry(self.root, default_tool_specs(), independent_tools=True)
        self.addCleanup(self.registry.context.executor.terminate_all)

    def test_exec_and_incremental_polling(self):
        first = self.registry.execute("exec_command", {"cmd": "printf first; sleep 0.1; printf second", "yield_time_ms": 0})
        self.assertTrue(first.ok, first.output)
        sid = first.structured["process_session"]["session_id"]
        done = self.registry.execute("write_stdin", {"session_id": sid, "yield_time_ms": 1000})
        self.assertTrue(done.ok, done.output)
        self.assertFalse(done.structured["process_session"]["running"])
        self.assertIn("firstsecond", first.stdout + done.stdout)
        again = self.registry.execute("write_stdin", {"session_id": sid, "yield_time_ms": 0})
        self.assertEqual(again.stdout, "")

    def test_process_input_and_unknown_handle(self):
        first = self.registry.execute("exec_command", {"cmd": "read value; printf 'got:%s' \"$value\"", "yield_time_ms": 0})
        sid = first.structured["process_session"]["session_id"]
        done = self.registry.execute("write_stdin", {"session_id": sid, "chars": "fixture\n", "yield_time_ms": 1000})
        self.assertTrue(done.ok, done.output); self.assertIn("got:fixture", done.stdout)
        self.assertEqual(self.registry.execute("write_stdin", {"session_id": "not-a-handle"}).failure_kind, "not_found")

    def test_exec_command_blocks_by_default(self):
        result = self.registry.execute("exec_command", {"cmd": "sleep 0.3; printf default-done"})
        self.assertTrue(result.ok, result.output)
        self.assertIn("default-done", result.stdout)
        self.assertFalse(result.structured["process_session"]["running"])

    def test_background_exec_returns_session(self):
        first = self.registry.execute("exec_command", {"cmd": "sleep 30", "background": True})
        self.assertTrue(first.ok, first.output)
        self.assertTrue(first.structured["process_session"]["running"])
        sid = first.structured["process_session"]["session_id"]
        self.registry.execute("write_stdin", {"session_id": sid, "chars": "\x03"})

    def test_blocking_exec_waits_for_exit(self):
        result = self.registry.execute(
            "exec_command", {"cmd": "sleep 0.3; printf blocked-done", "wait": True, "timeout_ms": 5000})
        self.assertTrue(result.ok, result.output)
        self.assertIn("blocked-done", result.stdout)
        self.assertFalse(result.structured["process_session"]["running"])

    def test_wait_process_until_exit_blocks(self):
        first = self.registry.execute("exec_command", {"cmd": "sleep 0.3; printf waved", "yield_time_ms": 0})
        handle = first.structured["process_session"]["session_id"]
        done = self.registry.execute("wait_process", {"handle": handle, "until_exit": True, "timeout_seconds": 5})
        self.assertTrue(done.ok, done.output)
        self.assertEqual(done.structured["process_wait"]["outcome"], "completed")

    def test_cmd_alias_is_authorized_before_execution(self):
        with patch.object(self.registry.context.executor, "start_process") as start:
            result = self.registry.execute("exec_command", {"cmd": "curl https://example.test"})
        self.assertEqual(result.failure_kind, "permission_approval_required")
        start.assert_not_called()

    def test_cancellation_and_unsupported_pty(self):
        first = self.registry.execute("exec_command", {"cmd": "python3 -c 'import time; time.sleep(30)'", "yield_time_ms": 0})
        sid = first.structured["process_session"]["session_id"]
        done = self.registry.execute("write_stdin", {"session_id": sid, "chars": "\x03"})
        self.assertFalse(done.structured["process_session"]["running"])
        self.assertEqual(self.registry.execute("exec_command", {"cmd": "true", "tty": True}).failure_kind, "unsupported_feature")

    def test_excessive_yield_and_long_sleep_are_rejected(self):
        too_long = self.registry.execute("exec_command", {
            "cmd": "true", "yield_time_ms": 30001,
        })
        self.assertFalse(too_long.ok)
        self.assertEqual(too_long.failure_kind, "invalid_input")
        self.assertIn("30000", too_long.output)

        blocked = self.registry.execute("exec_command", {
            "cmd": "sleep 31; true", "yield_time_ms": 0,
        })
        self.assertFalse(blocked.ok)
        self.assertEqual(blocked.failure_kind, "invalid_input")
        self.assertIn("long sleep", blocked.output)

        allowed = self.registry.execute("exec_command", {
            "cmd": "sleep 0.1; true", "yield_time_ms": 1000,
        })
        self.assertTrue(allowed.ok, allowed.output)

    def test_write_stdin_yield_is_capped(self):
        started = self.registry.execute("exec_command", {
            "cmd": "python3 -c 'import time; time.sleep(2)'", "yield_time_ms": 0,
        })
        sid = started.structured["process_session"]["session_id"]
        too_long = self.registry.execute("write_stdin", {
            "session_id": sid, "yield_time_ms": 30001,
        })
        self.assertFalse(too_long.ok)
        self.assertEqual(too_long.failure_kind, "invalid_input")

    def test_thirty_second_sleep_returns_control_without_killing_process(self):
        started = self.registry.execute("exec_command", {
            "cmd": "sleep 30; printf done", "yield_time_ms": 0,
        })
        self.assertTrue(started.ok, started.output)
        sid = started.structured["process_session"]["session_id"]
        self.assertTrue(started.structured["process_session"]["running"])
        stopped = self.registry.execute("write_stdin", {"session_id": sid, "chars": "\x03"})
        self.assertFalse(stopped.structured["process_session"]["running"])

    def test_plan_is_opt_in_advisory_and_persisted(self):
        self.assertNotIn("update_plan", self.registry.specs)
        with patch.dict("os.environ", {"HARNESS_ENABLE_TURN_PLAN": "1"}):
            registry = ToolRegistry(self.root, (), independent_tools=True)
        args = {"plan": [{"step": "check", "status": "in_progress"}]}
        result = registry.execute("update_plan", args)
        self.assertTrue(result.ok, result.output)
        saved = self.root / result.artifacts[0]
        self.assertEqual(json.loads(saved.read_text())["plan"], args["plan"])
        self.assertIsNone(registry.context.plan_store)
        self.assertFalse(registry.execute("update_plan", {"plan": args["plan"] * 2}).ok)

    def test_lsp_text_fallback_without_rg(self):
        (self.root / "a.ts").write_text("const value = 1\n")
        for spec in lsp_tool_specs(): self.registry.register(spec)
        with patch.object(self.registry.context.executor, "which", return_value=None):
            result = self.registry.execute("lsp_references", {"path": "a.ts", "symbol": "value"})
        self.assertTrue(result.ok, result.output)
        self.assertEqual(result.structured["fallback"], "search")
        self.assertIn("a.ts:1:", result.stdout)

    def test_unknown_before_tool_hooks_prevent_prefetch(self):
        runtime = AgentRuntime.__new__(AgentRuntime)
        tools = SimpleNamespace(dispatch=Mock())
        session = SimpleNamespace(tools=tools, hooks=HookPipeline((SimpleNamespace(name="custom", order=0),)))
        calls = [{"function": {"name": "read_file", "arguments": "{}"}}] * 2
        self.assertEqual(_prefetch_parallel_results(session, calls, None), {})
        tools.dispatch.assert_not_called()
