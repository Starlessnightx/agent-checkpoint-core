from __future__ import annotations

import json
import shlex
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from safety.boundaries import ensure_within

from .base import Command, ExecResult, ExecutorEnvironment, FileResult, ProcessStatus
from .local import LocalExecutor
from .history import CompletedProcessHistory, fresh_handle


Runner = Callable[..., subprocess.CompletedProcess[str]]


class DockerExecutor:
    """Executor whose commands run in a locked-down container with one workspace mount."""

    supports_process_stdin = False

    def __init__(
        self,
        workspace: Path,
        *,
        image: str = "python:3.12-alpine",
        docker: str = "docker",
        runner: Runner = subprocess.run,
    ):
        self.workspace = workspace.resolve()
        self.image = image
        self.docker = docker
        self._run = runner
        self._container: str | None = None
        self._files = LocalExecutor(self.workspace)
        self._processes: dict[str, dict[str, Any]] = {}
        self._completed_processes = CompletedProcessHistory()

    def inherit_completed_processes(self, records: tuple[ProcessStatus, ...]) -> None:
        if self._processes:
            raise RuntimeError('process history requires a fresh Executor')
        self._completed_processes.install(records, self.workspace)

    def fork(self, workspace: Path) -> "DockerExecutor":
        return DockerExecutor(
            workspace, image=self.image, docker=self.docker, runner=self._run
        )

    def _ensure_container(self) -> str:
        if self._container:
            return self._container
        name = "harness-sandbox-" + uuid.uuid4().hex[:12]
        result = self._run(
            [
                self.docker, "run", "--detach", "--rm", "--name", name,
                "--network", "none", "--read-only", "--cap-drop", "ALL",
                "--security-opt", "no-new-privileges", "--pids-limit", "256",
                "--tmpfs", "/tmp:rw,noexec,nosuid,size=64m",
                "--mount", f"type=bind,src={self.workspace},dst=/workspace,rw",
                "--workdir", "/workspace", self.image,
                "sh", "-c", "trap 'exit 0' TERM INT; while :; do sleep 3600; done",
            ],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"failed to start sandbox container: {result.stderr.strip()}")
        self._container = name
        return name

    def _inside(self, path: Path) -> str:
        resolved = ensure_within(path, self.workspace)
        relative = resolved.relative_to(self.workspace)
        return "/workspace" if not relative.parts else "/workspace/" + relative.as_posix()

    def exec(self, command: Command) -> ExecResult:
        container = self._ensure_container()
        cwd = self._inside(command.cwd)
        payload = (
            ["sh", "-lc", str(command.value)]
            if command.shell
            else [self._argument(str(value)) for value in command.value]
        )
        started = time.monotonic()
        try:
            result = self._run(
                [self.docker, "exec", "--workdir", cwd, container, *payload],
                text=True,
                capture_output=True,
                timeout=command.timeout_seconds,
                check=False,
            )
            return ExecResult(
                result.returncode, result.stdout or "", result.stderr or "",
                time.monotonic() - started,
            )
        except subprocess.TimeoutExpired as exc:
            return ExecResult(
                124,
                _text(exc.stdout),
                _text(exc.stderr),
                time.monotonic() - started,
                timed_out=True,
            )

    def _argument(self, value: str) -> str:
        candidate = Path(value)
        if not candidate.is_absolute():
            return value
        try:
            return self._inside(candidate)
        except PermissionError:
            return value

    def _path(self, path: Path) -> Path:
        return ensure_within(path, self.workspace)

    def read_file(self, path: Path) -> FileResult:
        return self._files.read_file(self._path(path))

    def write_file(self, path: Path, content: bytes) -> FileResult:
        return self._files.write_file(self._path(path), content)

    def append_file(self, path: Path, content: bytes) -> None:
        self._files.append_file(self._path(path), content)

    def hash_file(self, path: Path) -> str:
        return self._files.hash_file(self._path(path))

    def acquire_workspace_lease(self):
        return self._files.acquire_workspace_lease()

    def remove_file(self, path: Path) -> None:
        self._files.remove_file(self._path(path))

    def exists(self, path: Path) -> bool:
        return self._files.exists(self._path(path))

    def is_file(self, path: Path) -> bool:
        return self._files.is_file(self._path(path))

    def is_dir(self, path: Path) -> bool:
        return self._files.is_dir(self._path(path))

    def list_directory(self, path: Path) -> tuple[Path, ...]:
        return self._files.list_directory(self._path(path))

    def glob(self, root: Path, pattern: str) -> tuple[Path, ...]:
        return self._files.glob(self._path(root), pattern)

    def walk(self, root: Path) -> tuple[Path, ...]:
        return self._files.walk(self._path(root))

    def size(self, path: Path) -> int:
        return self._files.size(self._path(path))

    def snapshot(self, root: Path, limit: int = 20_000) -> dict[str, tuple[int, int]]:
        return self._files.snapshot(self._path(root), limit)

    def which(self, executable: str) -> str | None:
        if not executable or any(character in executable for character in "/\0\n"):
            return None
        result = self.exec(Command(f"command -v {shlex.quote(executable)}", self.workspace, 5, True))
        return result.stdout.strip() if result.return_code == 0 else None

    def inspect_environment(self) -> ExecutorEnvironment:
        script = (
            "printf '%s\\n' \"$(uname -s)\" \"$(uname -m)\"; "
            "python3 - <<'PY'\n"
            "import json, os\n"
            "keys=('PATH','LANG','SHELL','VIRTUAL_ENV')\n"
            "print(json.dumps({k: os.environ[k] for k in keys if k in os.environ}))\n"
            "print('1' if os.access('/workspace', os.W_OK) else '0')\n"
            "PY"
        )
        result = self.exec(Command(script, self.workspace, 5, True))
        lines = result.stdout.splitlines()
        if result.return_code or len(lines) < 4:
            return ExecutorEnvironment("unknown", "unknown", {}, False, "docker")
        try:
            environment = json.loads(lines[2])
        except json.JSONDecodeError:
            environment = {}
        return ExecutorEnvironment(lines[0], lines[1], environment, lines[3] == "1", "docker")

    def spawn_stdio(
        self, command: tuple[str, ...], cwd: Path
    ) -> subprocess.Popen[bytes]:
        container = self._ensure_container()
        payload = [self._argument(value) for value in command]
        return subprocess.Popen(
            [
                self.docker, "exec", "--interactive", "--workdir",
                self._inside(cwd), container, *payload,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def start_process(self, command: str, cwd: Path, log_dir: Path, *, stdin: bool = False) -> ProcessStatus:
        if stdin:
            raise NotImplementedError("detached DockerExecutor processes have no stdin")
        container = self._ensure_container()
        handle = fresh_handle(lambda: uuid.uuid4().hex[:12],
                              lambda key: key in self._processes or key in self._completed_processes)
        root = self._path(log_dir)
        root.mkdir(parents=True, exist_ok=True)
        log_path = root / f"{handle}.log"
        pid_path = root / f"{handle}.pid"
        exit_path = root / f"{handle}.exit"
        script = (
            f"sh -lc {shlex.quote(command)} > {shlex.quote(self._inside(log_path))} 2>&1 & "
            f"child=$!; echo $child > {shlex.quote(self._inside(pid_path))}; "
            f"wait $child; echo $? > {shlex.quote(self._inside(exit_path))}"
        )
        result = self._run(
            [self.docker, "exec", "--detach", "--workdir", self._inside(cwd),
             container, "sh", "-lc", script],
            text=True, capture_output=True, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"failed to start sandbox process: {result.stderr.strip()}")
        deadline = time.monotonic() + 5
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        pid = int(pid_path.read_text().strip()) if pid_path.exists() else -1
        self._processes[handle] = {
            "pid": pid, "command": command, "log": log_path,
            "pid_path": pid_path, "exit_path": exit_path,
        }
        return self.process_status(handle)  # type: ignore[return-value]

    def checkpoint_processes(self) -> tuple[ProcessStatus, ...]:
        result = list(self._completed_processes.records())
        for handle, value in tuple(self._processes.items()):
            exit_path = value["exit_path"]
            if exit_path.exists() and exit_path.stat().st_size > 64:
                raise ValueError("invalid managed process exit record")
            code = int(exit_path.read_text().strip()) if exit_path.exists() else None
            result.append(ProcessStatus(handle, value["pid"], value["command"], value["log"],
                                        code is None, code, ""))
        return tuple(result)

    def process_status(self, handle: str) -> ProcessStatus | None:
        if handle in self._completed_processes:
            return self._completed_processes.status(handle, lambda path: self.read_file(path).content)
        value = self._processes.get(handle)
        if value is None:
            return None
        exit_path = value["exit_path"]
        code = int(exit_path.read_text().strip()) if exit_path.exists() else None
        output = value["log"].read_text(errors="replace") if value["log"].exists() else ""
        return ProcessStatus(
            handle, value["pid"], value["command"], value["log"],
            code is None, code, output,
        )

    def stop_process(self, handle: str) -> ProcessStatus | None:
        if handle in self._completed_processes:
            return self.process_status(handle)
        value = self._processes.get(handle)
        if value is None:
            return None
        if not value["exit_path"].exists() and value["pid"] > 0 and self._container:
            self._run(
                [self.docker, "exec", self._container, "kill", "-TERM", str(value["pid"])],
                text=True, capture_output=True, check=False,
            )
        deadline = time.monotonic() + 2
        while not value["exit_path"].exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        return self.process_status(handle)

    def write_process_input(self, handle: str, data: str) -> ProcessStatus | None:
        if handle in self._completed_processes:
            raise ValueError('historical process is already exited and has no stdin')
        if handle not in self._processes:
            return None
        raise NotImplementedError("detached DockerExecutor processes have no stdin; use contained/local for interactive pipes")

    def terminate_all(self) -> None:
        for handle in tuple(self._processes):
            self.stop_process(handle)
        if self._container:
            self._run(
                [self.docker, "rm", "--force", self._container],
                text=True, capture_output=True, check=False,
            )
            self._container = None


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value or "")


__all__ = ["DockerExecutor"]
