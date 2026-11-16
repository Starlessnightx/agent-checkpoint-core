"""Trusted opt-in launcher policies; permission/sandbox kernel is unchanged."""
from __future__ import annotations

import os
from pathlib import Path
import re
import sys

from harness.permissions import PermissionDecision, PermissionLevel, PermissionPolicy, PermissionResult, Sandbox
from harness.permissions.sandbox import SandboxResult
from harness.tools.base import SideEffect

PERMISSION_PROFILES = ("restricted", "container-benchmark", "container-full-access")

# Defense in depth, not a shell parser or an OS sandbox. Arbitrary code requires
# a trusted outer container without host control sockets or sensitive mounts.
_HOST_CONTROL = re.compile(
    r"\b(?:sudo|su|doas|mount|umount|mkfs\w*|shutdown|reboot|"
    r"docker|podman|nsenter|ssh|scp)\b|docker\.sock|kill\s+-9\s+1\b",
    re.IGNORECASE,
)


def external_container_detected() -> bool:
    """Reject accidental host use; a marker is not security attestation."""
    return sys.platform == "linux" and (
        Path("/.dockerenv").is_file() or Path("/run/.containerenv").is_file()
    )


class ContainerBenchmarkPolicy(PermissionPolicy):
    profile_name = "container-benchmark"

    def __init__(self):
        # Keep the workspace permission ceiling. Preauthorization resolves ASK;
        # it must never convert a kernel DENY into permission to execute.
        super().__init__(approval=self._preauthorized)

    @staticmethod
    def _preauthorized(result, tool, request) -> bool:
        return (
            result.decision is PermissionDecision.ASK
            and tool.name in {"run_command", "start_process"}
            and not any(effect is SideEffect.EXTERNAL for effect in tool.side_effects)
            and not _HOST_CONTROL.search(str(request.arguments.get("command", "")))
        )

    def evaluate(self, tool, request, context):
        result = super().evaluate(tool, request, context)
        if result.decision is PermissionDecision.DENY:
            return result
        if _HOST_CONTROL.search(str(request.arguments.get("command", ""))):
            return PermissionResult(PermissionDecision.DENY,
                                    "container-benchmark excludes host-control operations",
                                    result.required, result.side_effects)
        # Record the selected authorization scope in existing permission events.
        return PermissionResult(result.decision,
                                "container-benchmark: " + result.reason,
                                result.required, result.side_effects)


class ContainerFullAccessPolicy(PermissionPolicy):
    """Trusted launch consent, not a claim that any command is safe."""
    profile_name = "container-full-access"
    preapprove_tool_hooks = True

    def __init__(self):
        super().__init__(PermissionLevel.FULL_ACCESS)

    def evaluate(self, tool, request, context):
        # Explicit child-role narrowing still applies; root has no inner ceiling.
        result = PermissionPolicy(self.maximum).evaluate(tool, request, context)
        if result.decision is PermissionDecision.DENY:
            return result
        return PermissionResult(PermissionDecision.ALLOW,
                                "container-full-access: launch-authorized; outer container is the boundary",
                                result.required, result.side_effects)


class ExternalContainerSandbox(Sandbox):
    filesystem_root = Path("/")

    def check(self, request, context):
        return SandboxResult(True, "inner sandbox disabled by explicit container-full-access launch")


def launcher_permission_policy(profile: str) -> PermissionPolicy | None:
    if profile not in PERMISSION_PROFILES:
        raise ValueError(f"unknown permission profile: {profile}")
    if profile == "restricted":
        return None  # Preserve existing default assembly exactly.
    if os.getenv("HARNESS_EXECUTOR", "local").strip().lower() != "contained":
        raise ValueError(f"{profile} requires HARNESS_EXECUTOR=contained")
    if not external_container_detected():
        raise ValueError(f"{profile} requires a detected external Linux container")
    return ContainerFullAccessPolicy() if profile == "container-full-access" else ContainerBenchmarkPolicy()


def launcher_sandbox(profile: str) -> Sandbox | None:
    # Repeat the same fail-closed launch validation before providing a bypass.
    launcher_permission_policy(profile)
    return ExternalContainerSandbox() if profile == "container-full-access" else None


def launcher_executor(profile: str, workspace: Path):
    launcher_permission_policy(profile)
    if profile == "container-full-access":
        from harness.executor import ContainedExecutor

        return ContainedExecutor(workspace, filesystem_root=Path("/"))
    return None
