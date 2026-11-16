from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

from harness.tools.base import SideEffect, ToolPermission, ToolRequest, ToolSpec

PermissionLevel = ToolPermission


class PermissionDecision(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    ASK = "ask"


@dataclass(frozen=True)
class PermissionResult:
    decision: PermissionDecision
    reason: str
    required: PermissionLevel
    side_effects: tuple[str, ...] = ()

    def dictionary(self) -> dict[str, Any]:
        return {
            "decision": self.decision.value,
            "reason": self.reason,
            "required": self.required.value,
            "side_effects": list(self.side_effects),
        }


_SENSITIVE = re.compile(
    r"(?:\bsudo\b|\bsu\b|\bdoas\b|\bmount\b|\bumount\b|\bmkfs\b|"
    r"\bshutdown\b|\breboot\b|docker\s+run|kill\s+-9\s+1|"
    r"\bcurl\b|\bwget\b|\bnc(?:at)?\b|\bssh\b|\bscp\b|\bftp\b|"
    r"git\s+(?:clone|fetch|pull)|\b(?:export|unset|source)\b|"
    r"\b(?:pip|npm|apt|brew|conda)\s+(?:install|uninstall|update)\b)",
    re.IGNORECASE,
)


class PermissionPolicy:
    """Evaluate tool metadata and arguments before any side effect occurs."""

    def __init__(self, maximum: PermissionLevel = PermissionLevel.WORKSPACE_WRITE,
                 approval: Callable[[PermissionResult, ToolSpec, ToolRequest], bool] | None = None):
        self.maximum = maximum
        self.approval = approval

    def evaluate(self, tool: ToolSpec, request: ToolRequest, context: Any) -> PermissionResult:
        required = tool.required_permission or PermissionLevel.READ_ONLY
        effects = tuple(sorted(effect.value for effect in tool.side_effects))
        rank = {PermissionLevel.READ_ONLY: 0, PermissionLevel.WORKSPACE_WRITE: 1,
                PermissionLevel.FULL_ACCESS: 2}
        if rank[required] > rank[self.maximum]:
            return PermissionResult(PermissionDecision.DENY,
                                    f"tool requires {required.value}, policy allows {self.maximum.value}",
                                    required, effects)
        command = str(request.arguments.get("command", ""))
        if command and _SENSITIVE.search(command):
            return PermissionResult(PermissionDecision.ASK,
                                    "command requests a sensitive external, network, or environment side effect",
                                    required, effects)
        if any(value in {SideEffect.NETWORK.value, SideEffect.ENVIRONMENT.value,
                         SideEffect.EXTERNAL.value} for value in effects):
            return PermissionResult(PermissionDecision.ASK,
                                    "tool declares an external side effect", required, effects)
        return PermissionResult(PermissionDecision.ALLOW, "within current permission policy", required, effects)

    def approve(self, result: PermissionResult, tool: ToolSpec, request: ToolRequest) -> bool:
        return bool(self.approval and self.approval(result, tool, request))
