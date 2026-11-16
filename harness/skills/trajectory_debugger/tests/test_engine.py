import json
import tempfile
import unittest
from pathlib import Path

from harness.skills.trajectory_debugger.engine import build_index, failure_report, query_index


class TrajectoryEngineTest(unittest.TestCase):
    def test_indexes_and_cites_repeated_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = [
                {"type": "tool", "name": "run_command", "arguments": {"command": "false"}, "ok": False, "failure_kind": "command_failed"},
                {"type": "tool", "name": "run_command", "arguments": {"command": "false"}, "ok": False, "failure_kind": "command_failed"},
                {"type": "tool", "name": "write_file", "arguments": {"path": "x"}, "ok": True, "state_delta": {"modified": ["x"]}},
            ]
            (root / "trace.json").write_text(json.dumps({"task_name": "demo", "trajectory": events}))
            index = build_index(root, ["trace.json"])
            self.assertEqual(index["metrics"]["event_count"], 3)
            self.assertEqual(len(query_index(index, ok=False)), 2)
            report = failure_report(index, "demo")
            self.assertTrue(report["repeated_failures"])
            self.assertIn("E-000001", report["markdown"])

    def test_failure_report_does_not_silently_truncate_large_episode(self):
        index = {
            "events": [
                {"event_id": f"E-{position:06d}", "task": "large", "ok": True}
                for position in range(1, 1002)
            ]
        }
        self.assertEqual(failure_report(index, "large")["event_count"], 1001)
