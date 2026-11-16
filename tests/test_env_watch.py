import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from harness import env_watch


def result(code=0, stdout=b""):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=b"")


class WatcherTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.run_dir = Path(self.directory.name) / "env-checkpoints"
        self.visible = {"cp1"}
        self.captured = []
        self.triggers = {"cp1": "agent_request: first", "cp2": "agent_request: second"}

    def runner(self, argv, input=None, timeout=600):
        if argv[:2] == ["docker", "ps"]:
            return result(0, b"task__abc\n")
        if argv[:2] == ["docker", "exec"] and "test" in argv and "-d" in argv:
            return result(0 if argv[-1] == "/app/.checkpoints" else 1)
        if argv[:2] == ["docker", "exec"] and "cat" in argv:
            name = argv[-1].split("/")[3]
            trigger = self.triggers.get(name)
            return result(0, json.dumps({"trigger": trigger}).encode())
        if argv[:2] == ["docker", "exec"] and "ls" in argv:
            return result(0, ("\n".join(sorted(self.visible)) + "\n").encode())
        if argv[:2] == ["docker", "exec"] and "test" in argv and "-f" in argv:
            name = argv[-1].split("/")[3]
            return result(0 if name in self.visible else 1)
        return result(0)

    def fake_capture(self, container, target, previous_dir=None, run=None):
        self.captured.append((container, Path(target).name, previous_dir))
        Path(target).mkdir(parents=True, exist_ok=True)
        return {"counts": {"changed": 1, "deleted": 0, "stored": 1, "linked": 0},
                "stored_bytes": 10}

    def test_captures_each_checkpoint_once(self):
        watcher = env_watch.EnvCheckpointWatcher("sha256:img", self.run_dir, runner=self.runner)
        with patch.object(env_watch.env_delta, "capture", side_effect=self.fake_capture), \
             patch.object(env_watch.env_delta, "record_environment", return_value={}):
            first = watcher.scan_once()
            second = watcher.scan_once()
            self.visible.add("cp2")
            third = watcher.scan_once()

        self.assertEqual([item["checkpoint"] for item in first], ["cp1"])
        self.assertEqual(second, [])
        self.assertEqual([item["checkpoint"] for item in third], ["cp2"])
        self.assertEqual(watcher.control, "/app/.checkpoints")
        self.assertEqual(len(self.captured), 2)
        self.assertEqual(self.captured[1][2].name, "cp1")  # chained to previous

    def test_step_checkpoints_capture_environment_too(self):
        self.visible = {"cp1", "cp2"}
        self.triggers["cp2"] = "step"
        watcher = env_watch.EnvCheckpointWatcher("sha256:img", self.run_dir, runner=self.runner)
        with patch.object(env_watch.env_delta, "capture", side_effect=self.fake_capture), \
             patch.object(env_watch.env_delta, "record_environment", return_value={}):
            captured = watcher.scan_once()
        self.assertEqual([item["checkpoint"] for item in captured], ["cp1", "cp2"])

    def test_missing_container_is_tolerated(self):
        def runner(argv, input=None, timeout=600):
            return result(0, b"")

        watcher = env_watch.EnvCheckpointWatcher("sha256:img", self.run_dir, runner=runner)
        self.assertEqual(watcher.scan_once(), [])
        self.assertIsNone(watcher.container)


if __name__ == "__main__":
    unittest.main()
