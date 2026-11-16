import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from science_checkpoint import env_delta


def _result(stdout=b"", stderr=b"", code=0):
    return SimpleNamespace(returncode=code, stdout=stdout, stderr=stderr)


class ParseDiffTest(unittest.TestCase):
    def test_mixed_entries(self):
        text = "A /app/new.txt\nC /etc/profile\nD /app/old.txt\nM /ignored\n"
        entries = env_delta.parse_diff(text)
        self.assertEqual([(e.kind, e.path) for e in entries],
                         [("A", "/app/new.txt"), ("C", "/etc/profile"), ("D", "/app/old.txt")])

    def test_ignores_malformed_lines(self):
        self.assertEqual(env_delta.parse_diff("\nnot-a-diff\nA relative/path\n"), [])


class DedupeTest(unittest.TestCase):
    def test_unchanged_file_is_hardlinked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            previous = root / "previous" / "envdiff"
            payload = root / "current" / "envdiff"
            (previous / "app").mkdir(parents=True)
            (payload / "app").mkdir(parents=True)
            (previous / "app" / "same.txt").write_text("stable")
            (payload / "app" / "same.txt").write_text("stable")
            (payload / "app" / "new.txt").write_text("fresh")

            record = env_delta._dedupe(payload, previous)

            self.assertTrue(record["app/same.txt"]["linked"])
            self.assertFalse(record["app/new.txt"]["linked"])
            self.assertEqual(os.stat(payload / "app" / "same.txt").st_ino,
                             os.stat(previous / "app" / "same.txt").st_ino)


class ExclusionTest(unittest.TestCase):
    def test_harness_state_is_excluded(self):
        self.assertTrue(env_delta._excluded("/app/.harness/sessions/x/trajectory.jsonl",
                                            env_delta.DEFAULT_EXCLUDES))
        self.assertTrue(env_delta._excluded("/installed-harness/main.py",
                                            env_delta.DEFAULT_EXCLUDES))
        self.assertFalse(env_delta._excluded("/app/results/table.csv",
                                             env_delta.DEFAULT_EXCLUDES))


class SymlinkTest(unittest.TestCase):
    def test_symlink_is_recorded_with_target(self):
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(directory) / "current" / "envdiff"
            payload.mkdir(parents=True)
            (payload / "a.txt").write_text("x")
            os.symlink("a.txt", payload / "link.txt")
            record = env_delta._dedupe(payload, None)
            self.assertEqual(record["link.txt"]["type"], "symlink")
            self.assertEqual(record["link.txt"]["target"], "a.txt")


class MaskedEnvironmentTest(unittest.TestCase):
    def test_secret_values_are_hashed(self):
        values = env_delta._masked_environment("PATH=/usr/bin\nMODEL_API_KEY=abc123\nLANG=C.UTF-8\n")
        self.assertEqual(values["PATH"], "/usr/bin")
        self.assertEqual(values["LANG"], "C.UTF-8")
        self.assertNotIn("abc123", json.dumps(values))
        self.assertIn("value_sha256", values["MODEL_API_KEY"])


class CaptureRestoreContractTest(unittest.TestCase):
    def test_capture_writes_manifest_and_deletions(self):
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "cp1"

            def runner(argv, input=None, timeout=600):
                if argv[:3] == ["docker", "diff", "box"]:
                    return _result(b"A /app/a.txt\nD /app/old.txt\n")
                if argv[:2] == ["docker", "exec"] and "tar" in argv:
                    import io
                    import tarfile
                    buffer = io.BytesIO()
                    with tarfile.open(fileobj=buffer, mode="w") as archive:
                        data = b"hello"
                        info = tarfile.TarInfo("app/a.txt")
                        info.size = len(data)
                        archive.addfile(info, io.BytesIO(data))
                    return _result(buffer.getvalue())
                return _result()

            manifest = env_delta.capture("box", out, run=runner)

            self.assertEqual(manifest["counts"], {"changed": 1, "deleted": 1, "stored": 1, "linked": 0})
            self.assertEqual(manifest["deletions"], ["/app/old.txt"])
            self.assertTrue((out / "envdiff" / "app" / "a.txt").is_file())
            self.assertEqual((out / "deletions.json").read_text().strip(), '[\n "/app/old.txt"\n]')


if __name__ == "__main__":
    unittest.main()
