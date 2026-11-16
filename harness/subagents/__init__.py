"""Budgeted subagents with isolated context and workspace modes."""

from .base import SubagentResult, SubagentSpec
from .coordinator import SubagentCoordinator
from .manager import AgentManager, AgentSpec

__all__ = ["AgentManager", "AgentSpec", "SubagentCoordinator", "SubagentResult", "SubagentSpec"]
