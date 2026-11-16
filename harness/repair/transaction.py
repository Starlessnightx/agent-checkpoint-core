from __future__ import annotations

import hashlib
import json
import shlex
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from harness.executor import Executor
from safety.boundaries import ensure_within


class RepairKind(str, Enum):
    USE_ALTERNATIVE_CAPABILITY = "UseAlternativeCapability"
    INSTALL_PYTHON_PACKAGE = "InstallPythonPackage"
    CHANGE_WORKING_DIRECTORY = "ChangeWorkingDirectory"
    SELECT_INTERPRETER = "SelectInterpreter"
    MODIFY_DEPENDENCY_MANIFEST = "ModifyDependencyManifest"


@dataclass(frozen=True)
class RepairAction:
    kind: str
    arguments: dict[str, Any]
    description: str = ""
    preconditions: tuple[str, ...] = ()
    permission: str = "read_only"
    side_effects: tuple[str, ...] = ()
    verification: tuple[str, ...] = ()
    rollback: str = "none"


@dataclass(frozen=True)
class RepairPolicy:
    name: str
    trigger: tuple[str, ...]
    preconditions: tuple[str, ...] = ()
    diagnosis: tuple[str, ...] = ()
    actions: tuple[RepairAction, ...] = ()
    verification: tuple[str, ...] = ()
    rollback: str = "restore_snapshot_on_any_verification_failure"


@dataclass(frozen=True)
class RepairSnapshot:
    files: dict[str, bytes]
    digest: str

    @classmethod
    def capture(cls, executor: Executor, workspace: Path, limit: int = 20_000) -> "RepairSnapshot":
        files: dict[str, bytes] = {}
        for path in executor.walk(workspace):
            if not executor.is_file(path) or ".harness" in path.parts:
                continue
            relative = str(path.relative_to(workspace))
            files[relative] = executor.read_file(path).content
            if len(files) >= limit:
                break
        canonical = b"".join(key.encode() + b"\0" + files[key] for key in sorted(files))
        return cls(files, hashlib.sha256(canonical).hexdigest())


@dataclass(frozen=True)
class RepairResult:
    status: str
    transaction_id: str
    diagnosis: str
    evidence: tuple[dict[str, Any], ...] = ()
    mutated: bool = False
    rollback_reason: str | None = None

    def dictionary(self) -> dict[str, Any]:
        return {"status": self.status, "transaction_id": self.transaction_id,
                "diagnosis": self.diagnosis, "evidence": list(self.evidence),
                "mutated": self.mutated, "rollback_reason": self.rollback_reason}


class RepairTransaction:
    def __init__(self, executor: Executor, workspace: Path, *,
                 dispatch: Callable[[str, dict[str, Any]], Any] | None = None,
                 journal: Path | None = None):
        self.executor, self.workspace = executor, workspace.resolve()
        self.dispatch = dispatch
        self.journal = journal
        self.transaction_id = "repair-" + hashlib.sha256(f"{time.time_ns()}".encode()).hexdigest()[:12]
        self.snapshot: RepairSnapshot | None = None
        self.events: list[dict[str, Any]] = []

    def begin(self) -> RepairSnapshot:
        self.snapshot = RepairSnapshot.capture(self.executor, self.workspace)
        self._record("snapshot", digest=self.snapshot.digest, files=len(self.snapshot.files))
        return self.snapshot

    def apply(self, action: RepairAction, *, dispatch: Callable[[str, dict[str, Any]], Any] | None = None) -> Any:
        if self.snapshot is None:
            raise RuntimeError("repair transaction must begin before apply")
        args = action.arguments
        checked_dispatch = dispatch or self.dispatch
        if checked_dispatch is None:
            raise RuntimeError(
                "repair mutation requires Registry dispatch through Permission and Sandbox"
            )
        if action.kind == RepairKind.INSTALL_PYTHON_PACKAGE.value:
            package = str(args["package"])
            interpreter = str(args.get("interpreter", "python3"))
            target = str(args.get("target", ".harness-env/python-packages"))
            result = checked_dispatch("run_command", {
                "command": (
                    f"{shlex.quote(interpreter)} -m pip install --target "
                    f"{shlex.quote(target)} {shlex.quote(package)}"
                ),
                "cwd": ".",
                "timeout_seconds": float(args.get("timeout_seconds", 120)),
            })
        elif action.kind in {
            RepairKind.USE_ALTERNATIVE_CAPABILITY.value,
            RepairKind.CHANGE_WORKING_DIRECTORY.value,
            RepairKind.SELECT_INTERPRETER.value,
        }:
            from harness.tools.base import ToolResult

            result = ToolResult(
                True,
                f"prepared typed repair action {action.kind}",
                structured={"repair_action": action.kind, "arguments": dict(args)},
            )
        elif action.kind == "write_file":
            path = ensure_within(self.workspace / str(args["path"]), self.workspace)
            result = checked_dispatch("write_file", {
                "path": str(path.relative_to(self.workspace)),
                "content": str(args.get("content", "")),
            })
        elif action.kind == "remove_file":
            path = ensure_within(self.workspace / str(args["path"]), self.workspace)
            relative = path.relative_to(self.workspace)
            result = checked_dispatch("run_command", {
                "command": "rm -f -- " + shlex.quote(str(relative)), "cwd": ".",
            })
        elif action.kind == "exec":
            cwd = ensure_within(self.workspace / str(args.get("cwd", ".")), self.workspace)
            result = checked_dispatch("run_command", {
                "command": str(args["command"]),
                "cwd": str(cwd.relative_to(self.workspace)) or ".",
                "timeout_seconds": float(args.get("timeout_seconds", 30)),
            })
        elif action.kind == "tool":
            result = checked_dispatch(str(args["name"]), dict(args.get("arguments", {})))
        else:
            raise ValueError(f"unsupported repair action: {action.kind}")
        self._record("apply", action=action.kind, description=action.description,
                     ok=getattr(result, "ok", True))
        return result

    def rollback(self, reason: str) -> None:
        if self.snapshot is None:
            raise RuntimeError("repair transaction has no snapshot")
        current = RepairSnapshot.capture(self.executor, self.workspace)
        for relative in set(current.files) - set(self.snapshot.files):
            self.executor.remove_file(ensure_within(self.workspace / relative, self.workspace))
        for relative, content in self.snapshot.files.items():
            self.executor.write_file(ensure_within(self.workspace / relative, self.workspace), content)
        self._record("rollback", reason=reason, restored_digest=self.snapshot.digest)

    def commit(self) -> None:
        self._record("commit", digest=RepairSnapshot.capture(self.executor, self.workspace).digest)

    def _record(self, event: str, **payload: Any) -> None:
        value = {"type": "repair", "transaction_id": self.transaction_id,
                 "event": event, **payload}
        self.events.append(value)
        if self.journal:
            self.journal.parent.mkdir(parents=True, exist_ok=True)
            with self.journal.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(value, ensure_ascii=False, default=str) + "\n")

    def run(self, policy: RepairPolicy, diagnosis: str, *,
            probe: Callable[[], bool] | None = None,
            rerun: Callable[[], bool] | None = None,
            regression: Callable[[], bool] | None = None,
            dispatch: Callable[[str, dict[str, Any]], Any] | None = None) -> RepairResult:
        self.begin()
        try:
            for action in policy.actions:
                result = self.apply(action, dispatch=dispatch)
                if hasattr(result, "ok") and not result.ok:
                    raise RuntimeError(f"repair action failed: {action.kind}")
            for label, check in (("targeted_probe", probe), ("failure_rerun", rerun), ("regression", regression)):
                if check is not None and not check():
                    raise RuntimeError(f"{label} failed")
                if check is not None:
                    self._record("verify", check=label, passed=True)
            self.commit()
            return RepairResult("committed", self.transaction_id, diagnosis,
                                tuple(self.events), mutated=bool(policy.actions))
        except Exception as exc:
            reason = str(exc)
            self.rollback(reason)
            return RepairResult("rolled_back", self.transaction_id, diagnosis,
                                tuple(self.events), mutated=bool(policy.actions),
                                rollback_reason=reason)
