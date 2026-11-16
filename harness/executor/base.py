from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence


@dataclass(frozen=True)
class Command:
    value: str | tuple[str, ...]
    cwd: Path
    timeout_seconds: float = 30
    shell: bool = False

    @classmethod
    def argv(
        cls, values: Sequence[str], cwd: Path, timeout_seconds: float = 30
    ) -> "Command":
        return cls(tuple(str(value) for value in values), cwd, timeout_seconds, False)


@dataclass(frozen=True)
class ExecResult:
    return_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False

    @property
    def returncode(self) -> int:
        """Compatibility with subprocess.CompletedProcess consumers."""
        return self.return_code


@dataclass(frozen=True)
class FileResult:
    path: Path
    content: bytes = b""
    existed: bool = False
    previous_size: int = 0


@dataclass(frozen=True)
class ProcessStatus:
    handle: str
    pid: int
    command: str
    log_path: Path
    running: bool
    return_code: int | None
    output: str = ""


@dataclass(frozen=True)
class ExecutorEnvironment:
    os: str
    arch: str
    environment: dict[str, str]
    workspace_writable: bool
    backend: str

    def dictionary(self) -> dict[str, Any]:
        return {
            "os": self.os,
            "arch": self.arch,
            "environment": dict(self.environment),
            "workspace_writable": self.workspace_writable,
            "backend": self.backend,
        }


class Executor(Protocol):
    workspace: Path

    def fork(self, workspace: Path) -> "Executor": ...
    def exec(self, command: Command) -> ExecResult: ...
    def read_file(self, path: Path) -> FileResult: ...
    def write_file(self, path: Path, content: bytes) -> FileResult: ...
    def append_file(self, path: Path, content: bytes) -> None: ...
    def hash_file(self, path: Path) -> str: ...
    def acquire_workspace_lease(self) -> Any: ...
    def remove_file(self, path: Path) -> None: ...
    def exists(self, path: Path) -> bool: ...
    def is_file(self, path: Path) -> bool: ...
    def is_dir(self, path: Path) -> bool: ...
    def list_directory(self, path: Path) -> tuple[Path, ...]: ...
    def glob(self, root: Path, pattern: str) -> tuple[Path, ...]: ...
    def walk(self, root: Path) -> tuple[Path, ...]: ...
    def size(self, path: Path) -> int: ...
    def snapshot(self, root: Path, limit: int = 20_000) -> dict[str, tuple[int, int]]: ...
    def which(self, executable: str) -> str | None: ...
    def inspect_environment(self) -> ExecutorEnvironment: ...
    def spawn_stdio(self, command: tuple[str, ...], cwd: Path) -> Any: ...
    def start_process(self, command: str, cwd: Path, log_dir: Path, *, stdin: bool = False) -> ProcessStatus: ...
    def process_status(self, handle: str) -> ProcessStatus | None: ...
    def checkpoint_processes(self) -> tuple[ProcessStatus, ...]: ...
    def inherit_completed_processes(self, records: tuple[ProcessStatus, ...]) -> None: ...
    def write_process_input(self, handle: str, data: str) -> ProcessStatus | None: ...
    def stop_process(self, handle: str) -> ProcessStatus | None: ...
    def terminate_all(self) -> None: ...


__all__ = [
    "Command", "ExecResult", "Executor", "ExecutorEnvironment", "FileResult",
    "ProcessStatus",
]
