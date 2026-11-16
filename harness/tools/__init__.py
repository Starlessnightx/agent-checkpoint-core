"""Typed, Python-registered tools available to the coding harness."""

from .base import (
    ArtifactRef,
    SideEffect,
    StateDelta,
    Tool,
    ToolContext,
    ToolFailure,
    ToolIntent,
    ToolPermission,
    ToolRequest,
    ToolResult,
    ToolSpec,
    VerificationEvidence,
)
from .router import RouteDecision, ToolRouter


def __getattr__(name):
    # Importing the permission kernel needs tools.base, not the whole registry.
    # Preserve public compatibility exports without a registry↔permissions cycle.
    from importlib import import_module

    if name in {"ToolRegistry", "default_tool_specs"}:
        return getattr(import_module("harness.tools.registry"), name)
    if name in {"PermissionDecision", "PermissionLevel", "PermissionPolicy", "PermissionResult", "Sandbox", "SandboxResult"}:
        return getattr(import_module("harness.permissions"), name)
    raise AttributeError(name)

__all__ = [
    "SideEffect",
    "ArtifactRef",
    "StateDelta",
    "Tool",
    "ToolContext",
    "ToolFailure",
    "ToolIntent",
    "ToolPermission",
    "ToolRequest",
    "ToolRegistry",
    "ToolResult",
    "ToolSpec",
    "VerificationEvidence",
    "ToolRouter",
    "RouteDecision",
    "default_tool_specs",
    "PermissionDecision",
    "PermissionLevel",
    "PermissionPolicy",
    "PermissionResult",
    "Sandbox",
    "SandboxResult",
]
