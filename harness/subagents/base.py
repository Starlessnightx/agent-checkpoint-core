from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Literal


WorkspaceMode = Literal["read_only", "shared_write", "patch_only"]


@dataclass(frozen=True)
class SubagentContextSpec:
    max_messages: int | None = None
    keep_first: int = 2
    keep_latest: int | None = None
    max_tool_output_chars: int = 12_000
    microcompact_only_under_pressure: bool = True
    summarize_omitted_actions: bool = True
    max_summary_chars: int | None = None
    compact_at_ratio: float | None = None
    compact_to_ratio: float | None = None
    emergency_compact_at_ratio: float | None = None


@dataclass(frozen=True)
class SubagentSpec:
    name: str
    description: str
    prompt_path: str
    tools: tuple[str, ...]
    skills: tuple[str, ...]
    context: SubagentContextSpec
    workspace_modes: tuple[WorkspaceMode, ...]
    default_workspace_mode: WorkspaceMode
    max_steps: int | None = None
    max_tool_calls: int | None = None
    max_context_tokens: int | None = None


@dataclass(frozen=True)
class SubagentResult:
    agent_id: str
    role: str
    status: Literal["completed", "failed", "cancelled"]
    summary: str
    workspace_mode: WorkspaceMode
    steps: int
    tool_calls: int
    patch: str = ""
    error: str | None = None
    stop_reason: str = "completed"
    trajectory_path: str = ""
    workspace_path: str = "."
    evidence_refs: tuple[str, ...] = ()
    verification: tuple[dict[str, Any] | Literal['not_evaluated'], ...] = ()
    artifacts: tuple[str, ...] = ()
    usage: dict[str, int] = field(default_factory=dict)
    compactions: int = 0
    events: tuple[dict[str, Any], ...] = ()

    def dictionary(self) -> dict[str, Any]:
        return asdict(self)

    def bounded_dictionary(self, *, max_summary_chars: int = 4_000,
                           max_patch_chars: int = 20_000) -> dict[str, Any]:
        """Parent-facing result; never includes the full child transcript."""
        value = self.dictionary()
        value.pop("events", None)
        value["summary"] = self.summary[:max_summary_chars]
        value["patch"] = self.patch[:max_patch_chars]
        return value
