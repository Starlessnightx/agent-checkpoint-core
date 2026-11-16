from __future__ import annotations

import os
from math import isfinite
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from harness.lsp import LSPServerConfig
    from harness.mcp import MCPServerConfig
    from harness.tools.base import ToolSpec


@dataclass(frozen=True)
class ModelSpec:
    api_mode: str = "chat_completions"
    responses_stream: bool = False
    responses_previous_response_id: bool = False
    responses_store: bool = False
    prompt_cache_retention: str | None = "24h"
    api_key_env: str = "MODEL_API_KEY"
    base_url_env: str = "MODEL_BASE_URL"
    model_env: str = "MODEL_NAME"
    default_base_url: str = "https://api.deepseek.com"
    default_model: str = "deepseek-v4-flash"
    request_timeout_seconds: float = 180
    retry_attempts: int = 4
    max_output_tokens_per_call: int | None = None
    context_window_tokens: int | None = None
    output_reserve_tokens: int | None = None
    parallel_tool_calls: bool = True
    input_usd_per_million: float | None = None
    cached_input_usd_per_million: float | None = None
    output_usd_per_million: float | None = None


@dataclass(frozen=True)
class BudgetSpec:
    max_tool_calls: int | None = None
    max_context_tokens: int | None = None
    max_parallel_subagents: int = 3
    max_cumulative_input_tokens: int | None = None
    max_cumulative_output_tokens: int | None = None
    max_cost_usd: float | None = None


@dataclass(frozen=True)
class ContextSpec:
    # Message-count limits are an opt-in compatibility fallback. Normal context
    # pressure is governed by the selected model's effective input capacity.
    max_messages: int | None = None
    keep_first: int = 2
    keep_latest: int | None = None
    max_recent_actions: int | None = None
    max_tool_output_chars: int = 16_000
    microcompact_only_under_pressure: bool = True
    summarize_omitted_actions: bool = True
    max_summary_chars: int | None = None
    compact_at_ratio: float | None = None
    compact_to_ratio: float | None = None
    emergency_compact_at_ratio: float | None = None


@dataclass(frozen=True)
class OrchestrationSpec:
    max_turns: int | None = None


@dataclass(frozen=True)
class RecoverySpec:
    max_tool_calls: int | None = None
    max_consecutive_failures: int = 4
    max_repeated_identical_calls: int = 2
    max_incomplete_responses: int = 3
    max_context_retries: int = 3
    stagnation_message: str = (
        "This action is repeating without progress. Use the recorded failure "
        "kind and evidence to choose a different approach."
    )
    repetition_message: str = (
        "The identical action has already been attempted. Change the arguments "
        "or test a different hypothesis."
    )
    failure_message: str = (
        "Several actions failed consecutively. Diagnose the latest concrete "
        "failure before attempting one materially different recovery action."
    )


@dataclass(frozen=True)
class HarnessSpec:
    root: Path
    workspace: Path
    prompt_path: Path
    model: ModelSpec = field(default_factory=ModelSpec)
    budget: BudgetSpec = field(default_factory=BudgetSpec)
    context: ContextSpec = field(default_factory=ContextSpec)
    orchestration: OrchestrationSpec = field(default_factory=OrchestrationSpec)
    recovery: RecoverySpec = field(default_factory=RecoverySpec)
    tools: tuple["ToolSpec", ...] = ()
    mcp_servers: tuple["MCPServerConfig", ...] = ()
    lsp_servers: tuple["LSPServerConfig", ...] = ()

    def inspect(self) -> dict:
        from harness.skills.registry import SkillRegistry
        from harness.subagents.registry import default_subagents

        return {
            "root": str(self.root),
            "workspace": str(self.workspace),
            "prompt_path": str(self.prompt_path),
            "model": self.model.__dict__,
            "budget": self.budget.__dict__,
            "context": self.context.__dict__,
            "orchestration": dict(self.orchestration.__dict__),
            "recovery": self.recovery.__dict__,
            "tools": [tool.name for tool in self.tools],
            "mcp_servers": [server.name for server in self.mcp_servers],
            "lsp_servers": [server.language for server in self.lsp_servers],
            "skills": SkillRegistry().summaries(),
            "subagents": [item.name for item in default_subagents()],
        }


def build_spec(root: Path, workspace: Path | None = None) -> HarnessSpec:
    """Build the complete harness from Python, without YAML/JSON policy files."""
    from harness.tools.registry import default_tool_specs

    resolved_root = root.resolve()
    override = os.getenv("HARNESS_WORKSPACE")
    selected_workspace = (
        Path(override) if override else workspace or resolved_root / "workspace"
    ).resolve()
    selected_workspace.mkdir(parents=True, exist_ok=True)
    max_calls = _optional_positive_int("HARNESS_MAX_TOOL_CALLS")
    return HarnessSpec(
        root=resolved_root,
        workspace=selected_workspace,
        prompt_path=resolved_root / "harness" / "prompts" / "system.md",
        budget=BudgetSpec(
            max_tool_calls=max_calls,
            max_context_tokens=_optional_positive_int("HARNESS_MAX_CONTEXT_TOKENS"),
            max_cumulative_input_tokens=_optional_positive_int(
                "HARNESS_MAX_CUMULATIVE_INPUT_TOKENS"
            ),
            max_cumulative_output_tokens=_optional_positive_int(
                "HARNESS_MAX_CUMULATIVE_OUTPUT_TOKENS"
            ),
            max_cost_usd=_optional_positive_float("HARNESS_MAX_COST_USD"),
        ),
        orchestration=OrchestrationSpec(
            max_turns=_optional_positive_int("HARNESS_MAX_TURNS")
        ),
        recovery=RecoverySpec(max_tool_calls=max_calls),
        tools=default_tool_specs(),
    )


def _optional_positive_int(name: str) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _optional_positive_float(name: str) -> float | None:
    raw = os.getenv(name)
    if raw is None:
        return None
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a finite positive number") from exc
    if not isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number")
    return value
