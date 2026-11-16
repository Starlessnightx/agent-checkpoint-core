"""Sequential two-run Harbor task controller with image-scoped cleanup.

The controller owns only a uniquely tagged task image. It deliberately never
calls ``docker system prune`` or removes an image while any container still
references it. Run directories are never deleted implicitly.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, Sequence


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class CommandRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> CommandResult: ...


class SubprocessRunner:
    def run(self, argv, *, cwd, env=None, timeout=None) -> CommandResult:
        completed = subprocess.run(
            list(argv),
            cwd=str(cwd),
            env=dict(env) if env is not None else None,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return CommandResult(completed.returncode, completed.stdout, completed.stderr)




def _read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _patch_docker_image(task_toml: Path, image_tag: str) -> None:
    text = task_toml.read_text(encoding="utf-8")
    line = f'docker_image = "{image_tag}"'
    existing = re.search(r"(?m)^docker_image\s*=.*$", text)
    if existing:
        text = text[:existing.start()] + line + text[existing.end():]
    else:
        section = re.search(r"(?m)^\[environment\]\s*$", text)
        if section is None:
            raise ValueError(f"{task_toml} has no [environment] section")
        insert_at = section.end()
        text = text[:insert_at] + "\n" + line + text[insert_at:]
    task_toml.write_text(text, encoding="utf-8")


@dataclass
class TaskRunController:
    """Build one task image, run Harbor twice, save both runs, then clean up."""

    root: Path
    task: Path
    runs_root: Path
    image_tag: str
    model: str
    agent: str = "harbor_agent:TerminalBenchHarnessAgent"
    agent_kwargs: tuple[str, ...] = ()
    env_file: Path | None = None
    platform: str | None = None
    harbor_command: tuple[str, ...] = ()
    run_timeout: float | None = None
    build_timeout: float | None = None
    runner: CommandRunner | None = None
    env: Mapping[str, str] | None = None
    run_id: str | None = None
    live_viewer: bool = False
    viewer_browser: bool = True
    prebuild_verifier: bool = True
    build_attempts: int = 1
    run_count: int = 2
    pause_on_error: bool = False

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self.task = self.task.resolve()
        self.runs_root = self.runs_root.resolve()
        if not self.task.is_dir():
            raise ValueError(f"task directory does not exist: {self.task}")
        if not (self.task / "task.toml").is_file():
            raise ValueError(f"task.toml missing in {self.task}")
        if not self.image_tag or any(ch.isspace() for ch in self.image_tag):
            raise ValueError("image_tag must be a non-empty Docker tag without whitespace")
        if self.run_count not in (1, 2):
            raise ValueError("run_count must be 1 or 2")
        if self.build_attempts < 1:
            raise ValueError("build_attempts must be positive")
        if not self.model:
            raise ValueError("model is required")
        self.runner = self.runner or SubprocessRunner()
        self.run_id = self.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self.harbor_command = tuple(self.harbor_command) or (
            str(Path(self.root, ".venv", "bin", "python")),
            "-m",
            "harbor.cli.main",
        )
        self._env = dict(os.environ if self.env is None else self.env)
        if self.env_file is not None:
            self._env.update(_read_env_file(self.env_file))
        self._image_id: str | None = None
        self._viewers = []
        self._registered_viewers: list[str] = []

    def _run(self, argv: Sequence[str], *, cwd: Path | None = None,
             timeout: float | None = None) -> CommandResult:
        return self.runner.run(
            tuple(str(part) for part in argv),
            cwd=cwd or self.root,
            env=self._env,
            timeout=timeout,
        )

    def _task_copy(self) -> Path:
        destination = self.runs_root / "task"
        if destination.exists():
            raise RuntimeError(f"task copy already exists: {destination}")
        self.runs_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(self.task, destination)
        _patch_docker_image(destination / "task.toml", self.image_tag)
        return destination

    def _inspect_image(self) -> tuple[str, list[str]]:
        result = self._run(
            ["docker", "image", "inspect", "--format", "{{.Id}}|{{json .RepoTags}}", self.image_tag]
        )
        if result.returncode != 0:
            raise RuntimeError(f"docker image inspect failed: {result.stderr.strip()[:500]}")
        first = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if "|" not in first:
            raise RuntimeError("docker image inspect returned no id/tags")
        image_id, raw_tags = first.split("|", 1)
        try:
            tags = json.loads(raw_tags)
        except json.JSONDecodeError as exc:
            raise RuntimeError("docker image inspect returned invalid tags") from exc
        if not image_id or not isinstance(tags, list):
            raise RuntimeError("docker image inspect returned incomplete identity")
        return image_id, [str(tag) for tag in tags]

    def build_image(self) -> dict[str, object]:
        dockerfile = self.task / "environment" / "Dockerfile"
        if not dockerfile.is_file():
            raise ValueError(f"environment/Dockerfile missing in {self.task}")
        command = [
            "docker", "build",
            "-t", self.image_tag,
            "--label", f"harness.task-run={self.run_id}",
        ]
        if self.platform:
            command.extend(["--platform", self.platform])
        command.extend(["-f", str(dockerfile), str(dockerfile.parent)])
        try:
            result = self._run(command, cwd=self.task / "environment", timeout=self.build_timeout)
        except subprocess.TimeoutExpired as exc:
            def as_text(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
            (self.runs_root / "build.stdout.log").write_text(as_text(exc.stdout), encoding="utf-8")
            (self.runs_root / "build.stderr.log").write_text(as_text(exc.stderr), encoding="utf-8")
            raise RuntimeError(f"task image build timed out after {self.build_timeout}s") from exc
        (self.runs_root / "build.stdout.log").write_text(result.stdout, encoding="utf-8")
        (self.runs_root / "build.stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"task image build failed with exit {result.returncode}")
        self._image_id, tags = self._inspect_image()
        if self.image_tag not in tags:
            raise RuntimeError(f"built image is not tagged as {self.image_tag}")
        return {"tag": self.image_tag, "image_id": self._image_id, "tags": tags}

    def prebuild_verifier_image(self, task_copy: Path) -> dict[str, object]:
        """Build the separate verifier image before the agent runs.

        Harbor builds the same Dockerfile again for the trial; because BuildKit
        caches by content, a successful warm-up makes that build a no-op. A
        failure here is reported before any model tokens are spent, instead of
        after a completed agent run.
        """
        directory = task_copy / "tests"
        dockerfile = directory / "Dockerfile"
        if not dockerfile.is_file():
            return {"status": "skipped", "reason": "tests/Dockerfile missing"}
        tag = f"harness-verifier-prebuilt-{self.run_id}:local"
        command = [
            "docker", "build",
            "-t", tag,
            "--label", f"harness.task-run={self.run_id}",
            "-f", str(dockerfile),
            str(directory),
        ]
        try:
            result = self._run(command, cwd=directory, timeout=self.build_timeout)
        except subprocess.TimeoutExpired as exc:
            def as_text(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
            (self.runs_root / "verifier-build.stdout.log").write_text(as_text(exc.stdout), encoding="utf-8")
            (self.runs_root / "verifier-build.stderr.log").write_text(as_text(exc.stderr), encoding="utf-8")
            raise RuntimeError(
                f"verifier image build timed out after {self.build_timeout}s"
            ) from exc
        (self.runs_root / "verifier-build.stdout.log").write_text(result.stdout, encoding="utf-8")
        (self.runs_root / "verifier-build.stderr.log").write_text(result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"verifier image build failed with exit {result.returncode}")
        # Drop only the tag; the shared build cache is what the trial reuses.
        removal = self._run(["docker", "image", "rm", tag])
        return {
            "status": "cached",
            "tag": tag,
            "tag_removed": removal.returncode == 0,
        }

    def _billing_paused(self, job_dir: Path) -> str | None:
        for status in job_dir.rglob("provider-status.json"):
            try:
                value = json.loads(status.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if value.get("status") == "waiting_for_billing":
                return str(status)
        for log in job_dir.rglob("*.jsonl"):
            try:
                with log.open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        if '"provider_paused"' in line or '"waiting_for_billing"' in line:
                            return str(log)
            except OSError:
                continue
        return None

    def _result_files(self, job_dir: Path) -> dict[str, list[str]]:
        def rel(paths):
            return [str(path.relative_to(job_dir)) for path in sorted(paths) if path.is_file()]
        return {
            "results": rel(job_dir.rglob("result.json")),
            "trajectories": rel(job_dir.rglob("harness-trajectory.jsonl")),
            "verifier": rel(job_dir.rglob("verifier/*.txt")) + rel(job_dir.rglob("verifier/*.json")),
        }

    def run_once(self, index: int, task_copy: Path) -> dict[str, object]:
        job_dir = self.runs_root / f"run-{index}"
        job_dir.mkdir(parents=True, exist_ok=False)
        command = [
            *self.harbor_command,
            "run",
            "--path", str(task_copy),
            "--agent", self.agent,
            "--model", self.model,
            "--n-attempts", "1",
            "--n-concurrent", "1",
            "--max-retries", "0",
            "--jobs-dir", str(job_dir),
            "--job-name", f"run-{index}",
            "--quiet",
        ]
        if self.env_file is not None:
            command.extend(["--env-file", str(self.env_file)])
        for value in self.agent_kwargs:
            command.extend(["--agent-kwarg", value])
        started = time.time()
        viewer_error = None
        viewer = None
        if self.live_viewer and self._image_id:
            try:
                from harness.frontends.task_live_viewer import TaskLiveViewer
                viewer = TaskLiveViewer(self.runner, self.root, self._image_id, job_dir,
                                        open_browser=self.viewer_browser)
                viewer.start()
                self._viewers.append(viewer)
                self._register_viewer(viewer)
            except Exception as exc:
                viewer_error = f"{type(exc).__name__}: {exc}"
                if viewer:
                    try:
                        viewer.close()
                    except Exception:
                        pass
                    viewer = None
        watcher = None
        env_checkpoints = None
        if os.environ.get("HARNESS_ENV_CHECKPOINTS", "1") == "1" and self._image_id:
            try:
                from harness.env_watch import EnvCheckpointWatcher
                watcher = EnvCheckpointWatcher(self._image_id, job_dir / "env-checkpoints")
                watcher.start()
            except Exception as exc:
                env_checkpoints = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
        try:
            # The live viewer is observational only: a browser or audit-mirror
            # failure must never abort a benchmark trial.
            result = self._run(command, timeout=self.run_timeout)
            error = None
        except subprocess.TimeoutExpired as exc:
            def as_text(value):
                return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else (value or "")
            result = CommandResult(124, as_text(exc.stdout), as_text(exc.stderr))
            error = f"run timeout after {self.run_timeout}s"
        except Exception as exc:  # preserve the failed attempt instead of losing it
            result = CommandResult(1, "", "")
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if watcher is not None:
                try:
                    env_checkpoints = watcher.stop()
                except Exception as exc:
                    env_checkpoints = {"status": "failed", "error": f"{type(exc).__name__}: {exc}"}
            if viewer:
                try:
                    viewer.finish()
                    if self.pause_on_error and not viewer.mirror:
                        viewer_error = "live viewer did not discover a unique task session"
                    elif self.pause_on_error and viewer.mirror.state.get("status") != "synced":
                        viewer_error = "live audit synchronization did not complete"
                except Exception as exc:
                    viewer_error = f"{type(exc).__name__}: {exc}"
        (job_dir / "harbor.stdout.log").write_text(result.stdout or "", encoding="utf-8")
        (job_dir / "harbor.stderr.log").write_text(result.stderr or "", encoding="utf-8")
        paused = self._billing_paused(job_dir)
        record = {
            "index": index,
            "job_dir": str(job_dir),
            "returncode": result.returncode,
            "elapsed_seconds": round(time.time() - started, 3),
            "billing_paused": bool(paused),
            "billing_evidence": paused,
            "error": error,
            "viewer_error": viewer_error,
            "env_checkpoints": env_checkpoints,
            **self._result_files(job_dir),
        }
        (job_dir / "run.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record

    def cleanup(self) -> dict[str, object]:
        if self._image_id is None:
            return {"status": "not_attempted", "reason": "image was not built"}
        image_id = self._image_id
        in_use = self._run(
            ["docker", "ps", "-a", "--filter", f"ancestor={image_id}", "--format", "{{.ID}}"]
        )
        if in_use.returncode != 0:
            return {"status": "skipped", "reason": "could not verify container usage",
                    "image_id": image_id, "stderr": in_use.stderr.strip()[:500]}
        containers = [line.strip() for line in in_use.stdout.splitlines() if line.strip()]
        if containers:
            return {"status": "skipped", "reason": "image is still used by containers",
                    "image_id": image_id, "containers": containers}
        try:
            actual_id, tags = self._inspect_image()
        except RuntimeError as exc:
            return {"status": "skipped", "reason": str(exc), "image_id": image_id}
        if actual_id != image_id:
            return {"status": "skipped", "reason": "image tag no longer resolves to built image",
                    "expected_image_id": image_id, "actual_image_id": actual_id}
        foreign = [tag for tag in tags if tag != self.image_tag]
        if foreign:
            return {"status": "skipped", "reason": "image has additional tags; refusing broad deletion",
                    "image_id": image_id, "extra_tags": foreign}
        removal = self._run(["docker", "image", "rm", image_id])
        if removal.returncode != 0:
            return {"status": "failed", "reason": "docker image rm failed", "image_id": image_id,
                    "stderr": removal.stderr.strip()[:500]}
        return {"status": "removed", "image_id": image_id, "tag": self.image_tag}

    def run(self) -> dict[str, object]:
        report: dict[str, object] = {
            "run_id": self.run_id,
            "task": str(self.task),
            "runs_root": str(self.runs_root),
            "model": self.model,
            "agent": self.agent,
            "image_tag": self.image_tag,
            "runs": [],
            "run_count": self.run_count,
            "status": "started",
        }
        task_copy = self._task_copy()
        report["task_copy"] = str(task_copy)
        report["build_errors"] = []
        for attempt in range(1, self.build_attempts + 1):
            try:
                report["image"] = self.build_image()
                break
            except Exception as exc:
                report["build_errors"].append(f"{type(exc).__name__}: {exc}")
                for name in ("build.stdout.log", "build.stderr.log"):
                    path = self.runs_root / name
                    if path.exists():
                        shutil.copyfile(path, self.runs_root / f"build-{attempt}.{name.split('.')[1]}.log")
        else:
            report["status"] = "build_failed"
            report["error"] = report["build_errors"][-1]
            self._write_report(report)
            return report
        if self.prebuild_verifier:
            try:
                report["verifier_image"] = self.prebuild_verifier_image(task_copy)
            except Exception as exc:
                # Fail before any agent tokens are spent; the build cache keeps
                # the successful layers for the next attempt.
                report["status"] = "verifier_build_failed"
                report["error"] = f"{type(exc).__name__}: {exc}"
                report["cleanup"] = self.cleanup()
                self._write_report(report)
                return report
        for index in range(1, self.run_count + 1):
            record = self.run_once(index, task_copy)
            report["runs"].append(record)  # type: ignore[union-attr]
            if record["billing_paused"]:
                report["status"] = "billing_paused"
                report["cleanup"] = {"status": "skipped", "reason": "provider billing pause"}
                self._write_report(report)
                self._close_viewers()
                return report
            if self.pause_on_error:
                from harness.task_batch import run_verified
                problem = record.get("error")
                if not problem and not run_verified(record):
                    problem = "run failed or did not produce a complete verifier result"
                if problem:
                    report["status"] = "paused"
                    report["error"] = f"run-{index}: {problem}"
                    report["cleanup"] = {"status": "skipped", "reason": "paused for investigation"}
                    self._write_report(report)
                    self._close_viewers()
                    return report
        report["status"] = "completed"
        missing = [
            run["index"] for run in report["runs"]
            if not run["results"] and not run["trajectories"]
        ]
        if missing:
            report["cleanup"] = {
                "status": "skipped",
                "reason": "one or more runs produced no saved result or trajectory",
                "runs_missing_artifacts": missing,
            }
        else:
            report["cleanup"] = self.cleanup()
        if self.pause_on_error and report["cleanup"].get("status") != "removed":
            report["status"] = "paused"
            report["error"] = f"cleanup: {report['cleanup'].get('reason', 'not completed')}"
        self._write_report(report)
        self._close_viewers()
        return report

    def _register_viewer(self, viewer) -> None:
        hub = os.environ.get("HARNESS_VIEWER_HUB")
        if not hub:
            return
        try:
            import urllib.request
            body = json.dumps({"id": self.run_id, "task": self.task.name,
                               "url": viewer.url}).encode()
            request = urllib.request.Request(
                hub.rstrip("/") + "/register", data=body,
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(request, timeout=1.5).read()
            self._registered_viewers.append(self.run_id)
        except Exception:
            pass

    def _unregister_viewers(self) -> None:
        hub = os.environ.get("HARNESS_VIEWER_HUB")
        if not hub:
            return
        import urllib.request
        for ident in self._registered_viewers:
            try:
                body = json.dumps({"id": ident}).encode()
                request = urllib.request.Request(
                    hub.rstrip("/") + "/unregister", data=body,
                    headers={"Content-Type": "application/json"})
                urllib.request.urlopen(request, timeout=1.5).read()
            except Exception:
                pass
        self._registered_viewers = []

    def _close_viewers(self) -> None:
        self._unregister_viewers()
        for viewer in self._viewers:
            try:
                viewer.close()
            except Exception:
                pass

    def _write_report(self, report: dict[str, object]) -> None:
        self.runs_root.mkdir(parents=True, exist_ok=True)
        (self.runs_root / "controller.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
        )
