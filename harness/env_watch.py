"""Host-side watcher that captures environment deltas at checkpoint boundaries.

The harness writes checkpoint requests into the control directory inside the
task container.  While the container runs, this watcher polls the control
directory and, for every new request, records the writable-layer delta with
``science_checkpoint.env_delta`` so dependencies and configuration changes are
stored next to the agent state.
"""
from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from science_checkpoint import env_delta

Runner = Callable[..., object]
CONTROL_CANDIDATES = ("/.checkpoints", "/app/.checkpoints", "/root/.checkpoints",
                      "/workspace/.checkpoints")


class EnvCheckpointWatcher:
    def __init__(self, image_id: str, run_dir: Path, *, interval: float = 2.0,
                 runner: Runner = env_delta.default_runner):
        self.image_id = image_id
        self.run_dir = Path(run_dir)
        self.interval = interval
        self.runner = runner
        self.container: str | None = None
        self.control: str | None = None
        self.previous: Path | None = None
        self.captures: list[dict] = []
        self.errors: list[str] = []
        self._seen: set[str] = set()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, name="env-checkpoints", daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
        return self.summary()

    def summary(self) -> dict:
        return {
            "container": self.container,
            "control": self.control,
            "captures": list(self.captures),
            "errors": list(self.errors),
        }

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.scan_once()
            except Exception as exc:  # never abort the trial for a capture failure
                self.errors.append(f"{type(exc).__name__}: {exc}")
            self._stop.wait(self.interval)

    def _find_container(self) -> str | None:
        if self.container:
            return self.container
        result = self.runner(["docker", "ps", "--filter", f"ancestor={self.image_id}",
                              "--format", "{{.Names}}"])
        names = [line.strip() for line in _text(result).splitlines() if line.strip()]
        names = [name for name in names if name.startswith("task__")]
        if names:
            self.container = names[0]
        return self.container

    def _find_control(self, container: str) -> str | None:
        if self.control:
            return self.control
        for candidate in CONTROL_CANDIDATES:
            probe = self.runner(["docker", "exec", container, "test", "-d", candidate])
            if getattr(probe, "returncode", 1) == 0:
                self.control = candidate
                return candidate
        return None

    def _bundle(self, container: str, control: str, name: str, target: Path) -> None:
        """Copy the host-side delta into the checkpoint so one archive is self-contained."""
        destination = f"{control}/{name}/env"
        self.runner(["docker", "exec", container, "mkdir", "-p", destination])
        copied = self.runner(["docker", "cp", str(target) + "/.", f"{container}:{destination}"])
        if getattr(copied, "returncode", 1) != 0:
            self.errors.append("env bundle failed: " + str(getattr(copied, "stderr", b""))[:200])

    def scan_once(self) -> list[dict]:
        container = self._find_container()
        if not container:
            return []
        control = self._find_control(container)
        if not control:
            return []
        listing = _text(self.runner(["docker", "exec", container, "ls", "-1", control]))
        names = {line.strip() for line in listing.splitlines() if line.strip()}
        captured: list[dict] = []
        for name in sorted(names - self._seen):
            probe = self.runner(["docker", "exec", container, "test", "-f",
                                 f"{control}/{name}/request.json"])
            if getattr(probe, "returncode", 1) != 0:
                continue
            self._seen.add(name)
            target = self.run_dir / name
            manifest = env_delta.capture(container, target, previous_dir=self.previous,
                                         run=self.runner)
            env_delta.record_environment(container, target, run=self.runner)
            self._bundle(container, control, name, target)
            self.previous = target
            record = {"checkpoint": name, "counts": manifest["counts"],
                      "stored_bytes": manifest["stored_bytes"]}
            self.captures.append(record)
            captured.append(record)
        return captured


def _text(result) -> str:
    stdout = getattr(result, "stdout", b"")
    return stdout.decode("utf-8", errors="replace") if isinstance(stdout, bytes) else str(stdout)
