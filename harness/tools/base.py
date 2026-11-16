from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Protocol

from harness.executor.base import Executor
from harness.media.images import ImageAttachment


class ToolPermission(str, Enum):
    """Declarative metadata only; enforcement is introduced in Phase 4."""

    READ_ONLY = "read_only"
    WORKSPACE_WRITE = "workspace_write"
    FULL_ACCESS = "full_access"


class ToolIntent(str, Enum):
    NORMAL = "normal"
    VERIFY = "verify"


class SideEffect(str, Enum):
    WORKSPACE = "workspace"
    PROCESS = "process"
    NETWORK = "network"
    ENVIRONMENT = "environment"
    EXTERNAL = "external"


@dataclass(frozen=True)
class ToolRequest:
    name: str
    arguments: dict[str, Any]
    call_id: str = ""
    intent: ToolIntent = ToolIntent.NORMAL


@dataclass(frozen=True)
class ToolFailure:
    kind: str
    message: str
    retryable: bool = False
    details: dict[str, Any] | None = None

    def dictionary(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "message": self.message,
            "retryable": self.retryable,
            "details": self.details,
        }


@dataclass(frozen=True)
class StateDelta:
    created: tuple[str, ...] = ()
    modified: tuple[str, ...] = ()
    deleted: tuple[str, ...] = ()
    processes_started: tuple[str, ...] = ()
    processes_stopped: tuple[str, ...] = ()

    def dictionary(self) -> dict[str, list[str]]:
        return {
            "created": list(self.created),
            "modified": list(self.modified),
            "deleted": list(self.deleted),
            "processes_started": list(self.processes_started),
            "processes_stopped": list(self.processes_stopped),
        }


@dataclass(frozen=True)
class ArtifactRef:
    path: str
    kind: str = "file"
    digest: str | None = None

    def dictionary(self) -> dict[str, Any]:
        return {"path": self.path, "kind": self.kind, "digest": self.digest}


@dataclass(frozen=True)
class VerificationEvidence:
    target: str
    command: str
    passed: bool
    exit_code: int | None = None
    duration_seconds: float | None = None
    artifacts: tuple[ArtifactRef, ...] = ()
    check_type: str = "command"
    details: dict[str, Any] | None = None
    artifact_hash: str | None = None

    def dictionary(self) -> dict[str, Any]:
        return {
            "target": self.target, "command": self.command, "passed": self.passed,
            "exit_code": self.exit_code, "duration_seconds": self.duration_seconds,
            "artifacts": [item.dictionary() for item in self.artifacts],
            "check_type": self.check_type,
            "details": self.details,
            "artifact_hash": self.artifact_hash,
        }


@dataclass(frozen=True)
class ToolResult:
    ok: bool
    summary: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    artifacts: tuple[str, ...] = ()
    state_delta: StateDelta | None = None
    failure_kind: str | None = None
    retryable: bool = False
    verification: dict[str, Any] | None = None
    structured: dict[str, Any] | None = None
    failure: ToolFailure | None = None
    artifact_refs: tuple[ArtifactRef, ...] = ()
    verification_evidence: VerificationEvidence | None = None
    images: tuple[ImageAttachment, ...] = ()

    def __post_init__(self) -> None:
        if self.ok and (self.failure is not None or self.failure_kind is not None):
            raise ValueError("successful ToolResult cannot contain a failure")
        if self.failure is not None:
            object.__setattr__(self, "failure_kind", self.failure.kind)
            object.__setattr__(self, "retryable", self.failure.retryable)
        elif not self.ok:
            failure = ToolFailure(
                kind=self.failure_kind or "tool_failed",
                message=self.summary,
                retryable=self.retryable,
            )
            object.__setattr__(self, "failure", failure)

    @property
    def output(self) -> str:
        parts = [self.summary]
        if self.exit_code is not None:
            parts.append(f"exit_code={self.exit_code}")
        if self.stdout:
            parts.append(self.stdout)
        if self.stderr:
            parts.append(f"[stderr]\n{self.stderr}")
        if self.artifacts:
            parts.append("artifacts=" + ", ".join(self.artifacts))
        if self.state_delta:
            delta = self.state_delta.dictionary()
            rendered = ", ".join(
                f"{name}={paths}" for name, paths in delta.items() if paths
            )
            if rendered:
                parts.append("state_delta=" + rendered)
        if self.failure_kind:
            parts.append(f"failure_kind={self.failure_kind}")
        return "\n".join(part for part in parts if part)


@dataclass
class ToolContext:
    workspace: Path
    artifact_root: Path
    process_registry: Any
    output_soft_limit: int = 16_000
    executor: Executor | None = None
    interrupt_wait: Callable[[], bool] | None = None
    background_tasks: Any = None
    plan_store: Any = None
    process_started_times: dict[str, float] = field(default_factory=dict)
    image_input_support: Callable[[], bool | None] | None = None
    filesystem_root: Path | None = None
    exec_output_offsets: dict[str, int] = field(default_factory=dict)
    interaction: ToolInteraction | None = None

    def __post_init__(self) -> None:
        if self.executor is None:
            from harness.executor.factory import create_executor

            self.executor = create_executor(self.workspace)

    def relative(self, path: Path) -> str:
        resolved = path.resolve()
        if resolved.is_relative_to(self.workspace.resolve()):
            return str(resolved.relative_to(self.workspace.resolve()))
        if self.filesystem_root is not None and resolved.is_relative_to(self.filesystem_root.resolve()):
            return str(resolved)
        raise PermissionError(f"path escapes boundary: {path}")


ToolHandler = Callable[[ToolContext, dict[str, Any]], ToolResult]


class ToolInteraction(Protocol):
    """Runtime-injected channel; tools never access Session or the model."""

    def ask(self, questions: list[dict[str, str]]) -> ToolResult: ...

    def approve_hook(self, name: str, arguments: dict[str, Any], call_id: str,
                     reason: str) -> bool: ...


class Tool(Protocol):
    spec: "ToolSpec"

    def execute(self, request: ToolRequest, context: ToolContext) -> ToolResult: ...


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    input_schema: dict[str, Any]
    handler: ToolHandler
    capability: str
    mutates_workspace: bool = False
    timeout_seconds: float = 30
    required_permission: ToolPermission | None = None
    side_effects: frozenset[SideEffect] = frozenset()
    metadata: dict[str, Any] = field(default_factory=dict)
    permission_argument_aliases: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        permission = self.required_permission
        effects = self.side_effects
        if permission is None:
            permission = (
                ToolPermission.WORKSPACE_WRITE
                if self.mutates_workspace or _process_can_mutate(self.capability)
                else ToolPermission.READ_ONLY
            )
            object.__setattr__(self, "required_permission", permission)
        if not effects:
            inferred: set[SideEffect] = set()
            if self.mutates_workspace:
                inferred.add(SideEffect.WORKSPACE)
            if _process_capability(self.capability):
                inferred.add(SideEffect.PROCESS)
            if _process_can_mutate(self.capability):
                inferred.add(SideEffect.WORKSPACE)
            object.__setattr__(self, "side_effects", frozenset(inferred))

    def schema(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }

    def execute(self, request: ToolRequest, context: ToolContext) -> ToolResult:
        if request.name != self.name:
            return ToolResult(
                False,
                f"tool request name mismatch: expected {self.name}, got {request.name}",
                failure=ToolFailure(
                    "tool_mismatch",
                    f"expected {self.name}, got {request.name}",
                ),
            )
        return self.handler(context, request.arguments)


def _process_capability(capability: str) -> bool:
    return capability.startswith("process:") or capability in {
        "verification:run", "harness:test",
    }


def _process_can_mutate(capability: str) -> bool:
    return capability in {
        "process:run", "process:start", "process:stop",
        "verification:run", "harness:test",
    }


def object_schema(
    properties: dict[str, Any],
    required: tuple[str, ...] = (),
) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }
