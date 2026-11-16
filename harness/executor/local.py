from __future__ import annotations

import os
import hashlib
import fcntl
import platform
import shutil
import signal
import select
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

from safety.boundaries import ensure_within

from .base import Command, ExecResult, ExecutorEnvironment, FileResult, ProcessStatus
from .history import CompletedProcessHistory, fresh_handle


@dataclass
class _LocalProcess:
    handle: str
    process: subprocess.Popen[str]
    command: str
    log_path: Path
    stream: IO[str]


class WorkspaceLease:
    def __init__(self, stream):
        self.stream = stream

    def close(self) -> None:
        if not self.stream.closed:
            fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
            self.stream.close()


class LocalExecutor:
    """Unsafe development backend; use only inside an existing trusted sandbox."""

    supports_process_stdin = True

    def __init__(self, workspace: Path):
        self.workspace = workspace.resolve()
        self.filesystem_root = self.workspace
        self._processes: dict[str, _LocalProcess] = {}
        self._completed_processes = CompletedProcessHistory()

    def inherit_completed_processes(self, records: tuple[ProcessStatus, ...]) -> None:
        if self._processes:
            raise RuntimeError('process history requires a fresh Executor')
        self._completed_processes.install(records, self.workspace)

    def fork(self, workspace: Path) -> "LocalExecutor":
        return LocalExecutor(workspace)

    def checkpoint_processes(self) -> tuple[ProcessStatus, ...]:
        """Managed process facts only; not proof of OS-wide quiescence."""
        result = list(self._completed_processes.records())
        for handle, managed in tuple(self._processes.items()):
            code = managed.process.poll()
            result.append(ProcessStatus(handle, managed.process.pid, managed.command,
                                        managed.log_path, code is None, code, ""))
        return tuple(result)

    def exec(self, command: Command) -> ExecResult:
        started = time.monotonic()
        process = subprocess.Popen(
            command.value,
            cwd=command.cwd,
            shell=command.shell,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        timed_out = False
        try:
            stdout, stderr = process.communicate(timeout=command.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            self._terminate(process, 3)
            stdout, stderr = process.communicate()
        return ExecResult(
            return_code=int(process.returncode or 0),
            stdout=stdout or "",
            stderr=stderr or "",
            duration_seconds=time.monotonic() - started,
            timed_out=timed_out,
        )

    def read_file(self, path: Path) -> FileResult:
        return FileResult(path=path, content=path.read_bytes(), existed=True)

    def write_file(self, path: Path, content: bytes) -> FileResult:
        existed = path.exists()
        previous_size = path.stat().st_size if path.is_file() else 0
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return FileResult(path, content, existed, previous_size)

    def remove_file(self, path: Path) -> None:
        path.unlink(missing_ok=True)

    def append_file(self, path: Path, content: bytes) -> None:
        ensure_within(path, self.filesystem_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("ab") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())

    def hash_file(self, path: Path) -> str:
        ensure_within(path, self.filesystem_root)
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def acquire_workspace_lease(self) -> WorkspaceLease:
        path = ensure_within(self.workspace / ".harness" / "workspace.lock", self.workspace)
        path.parent.mkdir(parents=True, exist_ok=True)
        stream = path.open("a+b")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise RuntimeError("workspace has an active turn session; concurrent shared-workspace execution is forbidden") from exc
        return WorkspaceLease(stream)

    def exists(self, path: Path) -> bool:
        return path.exists()

    def is_file(self, path: Path) -> bool:
        return path.is_file()

    def is_dir(self, path: Path) -> bool:
        return path.is_dir()

    def list_directory(self, path: Path) -> tuple[Path, ...]:
        return tuple(path.iterdir())

    def glob(self, root: Path, pattern: str) -> tuple[Path, ...]:
        return tuple(root.glob(pattern))

    def walk(self, root: Path) -> tuple[Path, ...]:
        return tuple(root.rglob("*"))

    def size(self, path: Path) -> int:
        return path.stat().st_size

    def snapshot(
        self, root: Path, limit: int = 20_000
    ) -> dict[str, tuple[int, int]]:
        snapshot: dict[str, tuple[int, int]] = {}
        for path in root.rglob("*"):
            relative = path.relative_to(root)
            if relative.parts and relative.parts[0] == ".harness":
                continue
            if not path.is_file():
                continue
            stat = path.stat()
            snapshot[str(relative)] = (stat.st_size, stat.st_mtime_ns)
            if len(snapshot) >= limit:
                break
        return snapshot

    def which(self, executable: str) -> str | None:
        return shutil.which(executable)

    def inspect_environment(self) -> ExecutorEnvironment:
        return ExecutorEnvironment(
            os=platform.system(),
            arch=platform.machine(),
            environment={
                key: os.environ.get(key, "")
                for key in ("PATH", "LANG", "SHELL", "VIRTUAL_ENV")
                if key in os.environ
            },
            workspace_writable=os.access(self.workspace, os.W_OK),
            backend="local-unsafe",
        )

    def spawn_stdio(
        self, command: tuple[str, ...], cwd: Path
    ) -> subprocess.Popen[bytes]:
        return subprocess.Popen(
            command,
            cwd=ensure_within(cwd, self.filesystem_root),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )

    def start_process(self, command: str, cwd: Path, log_dir: Path, *, stdin: bool = False) -> ProcessStatus:
        handle = fresh_handle(lambda: uuid.uuid4().hex[:12],
                              lambda key: key in self._processes or key in self._completed_processes)
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / f"{handle}.log"
        stream = log_path.open("w", encoding="utf-8")
        process = subprocess.Popen(
            command,
            cwd=cwd,
            shell=True,
            text=True,
            stdout=stream,
            stdin=subprocess.PIPE if stdin else subprocess.DEVNULL,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        self._processes[handle] = _LocalProcess(
            handle, process, command, log_path, stream
        )
        return self._status(self._processes[handle])

    def process_status(self, handle: str) -> ProcessStatus | None:
        if handle in self._completed_processes:
            return self._completed_processes.status(handle, lambda path: self.read_file(path).content)
        managed = self._processes.get(handle)
        return self._status(managed) if managed else None

    def write_process_input(self, handle: str, data: str) -> ProcessStatus | None:
        if handle in self._completed_processes:
            raise ValueError('historical process is already exited and has no stdin')
        managed = self._processes.get(handle)
        if managed is None:
            return None
        if managed.process.poll() is not None:
            raise ValueError("process already exited")
        if managed.process.stdin is None:
            raise ValueError("process has no writable stdin")
        fd = managed.process.stdin.fileno()
        raw = data.encode("utf-8")
        if len(raw) > os.fpathconf(fd, "PC_PIPE_BUF"):
            raise ValueError("stdin payload exceeds atomic pipe limit; send a smaller chunk")
        if not select.select([], [fd], [], 1.0)[1]:
            raise TimeoutError("stdin is not writable; input was not sent")
        previous = os.get_blocking(fd)
        try:
            os.set_blocking(fd, False)
            written = os.write(fd, raw)
            if written != len(raw):
                raise RuntimeError(f"only {written} stdin bytes sent; do not blindly retry")
        finally:
            os.set_blocking(fd, previous)
        return self._status(managed)

    def stop_process(self, handle: str) -> ProcessStatus | None:
        if handle in self._completed_processes:
            return self.process_status(handle)
        managed = self._processes.get(handle)
        if not managed:
            return None
        if managed.process.poll() is None:
            self._terminate(managed.process, 5)
        managed.stream.close()
        return self._status(managed)

    def terminate_all(self) -> None:
        for handle in tuple(self._processes):
            try:
                self.stop_process(handle)
            except OSError:
                continue

    def _status(self, managed: _LocalProcess) -> ProcessStatus:
        code = managed.process.poll()
        if not managed.stream.closed:
            if code is None:
                managed.stream.flush()
            else:
                managed.stream.close()
                if managed.process.stdin is not None:
                    managed.process.stdin.close()
        try:
            output = managed.log_path.read_text(errors="replace")
        except OSError:
            output = ""
        return ProcessStatus(
            handle=managed.handle,
            pid=managed.process.pid,
            command=managed.command,
            log_path=managed.log_path,
            running=code is None,
            return_code=code,
            output=output,
        )

    @staticmethod
    def _terminate(process: subprocess.Popen[Any], timeout: float) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=timeout)
        except (ProcessLookupError, subprocess.TimeoutExpired):
            if process.poll() is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait(timeout=timeout)
                except (ProcessLookupError, subprocess.TimeoutExpired):
                    pass


class ContainedExecutor(LocalExecutor):
    """Local process adapter for a workspace already isolated by its platform."""

    def __init__(self, workspace: Path, *, filesystem_root: Path | None = None):
        super().__init__(workspace)
        if filesystem_root is not None:
            self.filesystem_root = filesystem_root.resolve()

    def fork(self, workspace: Path) -> "ContainedExecutor":
        root = self.filesystem_root if self.filesystem_root != self.workspace else None
        return ContainedExecutor(workspace, filesystem_root=root)

    def inspect_environment(self) -> ExecutorEnvironment:
        value = super().inspect_environment()
        return ExecutorEnvironment(
            value.os, value.arch, value.environment, value.workspace_writable,
            "external-container",
        )


__all__ = ["ContainedExecutor", "LocalExecutor"]
