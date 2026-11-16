from __future__ import annotations

import json
from hashlib import sha256
from pathlib import Path
from typing import Any, Callable

from harness.tools.base import ToolContext, ToolPermission, ToolRequest, ToolSpec, ToolResult
from harness.executor.base import Executor, Command
from harness.permissions.policy import PermissionPolicy, PermissionDecision
from harness.permissions.sandbox import Sandbox


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"), allow_nan=False).encode()


def digest(value: bytes) -> str:
    return sha256(value).hexdigest()


class ExperimentIO:
    """All experimental filesystem effects pass existing permission boundaries."""
    def __init__(self, executor: Executor, policy: PermissionPolicy,
                 emit: Callable[..., Any] | None = None):
        self.executor, self.policy = executor, policy
        self.context = ToolContext(executor.workspace, executor.workspace, None,
                                   executor=executor)
        self.emit = emit or (lambda *args, **kwargs: None)

    def check(self, path: Path, write: bool = False) -> Path:
        path = path if path.is_absolute() else self.executor.workspace / path
        # Check both lexical and resolved targets, including symlink parents.
        request = ToolRequest("experiment_write" if write else "experiment_read",
                              {"path": str(path)})
        spec = ToolSpec(request.name, "Experimental state I/O", {},
                        lambda context, args: ToolResult(True, "checked"), "experiment:state",
                        mutates_workspace=write,
                        required_permission=(ToolPermission.WORKSPACE_WRITE if write
                                             else ToolPermission.READ_ONLY))
        decision = self.policy.evaluate(spec, request, self.context)
        sandbox = Sandbox().check(request, self.context)
        self.emit("experiment_permission", path=str(path), write=write,
                  permission=decision.dictionary(), sandbox=sandbox.dictionary())
        if not sandbox.allowed or decision.decision == PermissionDecision.DENY:
            raise PermissionError(sandbox.reason if not sandbox.allowed else decision.reason)
        if decision.decision == PermissionDecision.ASK and not self.policy.approve(decision, spec, request):
            raise PermissionError(decision.reason)
        return path

    def read(self, path: Path) -> bytes:
        return self.executor.read_file(self.check(path)).content

    def write(self, path: Path, value: bytes) -> None:
        self.executor.write_file(self.check(path, True), value)


    def put(self, root: Path, value: bytes) -> str:
        identifier = digest(value)
        path = root / identifier
        self.check(path, True)
        if self.executor.exists(path):
            if self.read(path) != value:
                raise ValueError("immutable content collision or corrupted object")
        else:
            self.write(path, value)
        return identifier

    def get(self, root: Path, identifier: str) -> bytes:
        if len(identifier) != 64 or any(c not in "0123456789abcdef" for c in identifier):
            raise ValueError("invalid content identifier")
        value = self.read(root / identifier)
        if digest(value) != identifier:
            raise ValueError("checkpoint object hash mismatch")
        return value
