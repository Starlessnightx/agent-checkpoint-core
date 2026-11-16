"""Describe effective launch authority without granting any new permissions."""
from dataclasses import dataclass
from datetime import datetime, timezone
import json

from harness.permissions import PermissionLevel
from harness.trusted.deployment import ContainerFullAccessPolicy, ExternalContainerSandbox


@dataclass(frozen=True)
class LaunchContext:
    cwd: str
    filesystem_root: str
    permission_profile: str
    permission_ceiling: str
    approval_policy: str
    network_authorization: str
    inner_sandbox: str
    current_date: str
    timezone: str = "UTC (runtime clock)"
    shell: str = "exec_command defaults to sh; bash can be selected when installed"

    def dictionary(self):
        from dataclasses import asdict
        return asdict(self)

    def prompt(self):
        return ("<permissions instructions>\n"
                + json.dumps(self.dictionary(), ensure_ascii=False, indent=2)
                + "\nThese values describe the effective launcher configuration, not a grant of extra authority. "
                "Network authorization does not guarantee connectivity. Tools and explicit child-role limits still apply. "
                "Do not request sandbox_permissions escalation; it is not supported by this harness.\n"
                "</permissions instructions>\n"
                "<environment_context>\n"
                + json.dumps({"cwd": self.cwd, "workspace_roots": [self.cwd],
                              "shell": self.shell, "current_date": self.current_date,
                              "timezone": self.timezone}, ensure_ascii=False, indent=2)
                + "\n</environment_context>")


def describe_launch(tools, *, now=None):
    policy, sandbox, context = tools.permission_policy, tools.sandbox, tools.context
    full = isinstance(policy, ContainerFullAccessPolicy) and policy.maximum is PermissionLevel.FULL_ACCESS
    return LaunchContext(
        cwd=str(context.workspace), filesystem_root=str(context.filesystem_root or context.workspace),
        permission_profile=getattr(policy, "profile_name", "restricted"),
        permission_ceiling=policy.maximum.value,
        approval_policy="never; launch-preauthorized" if full else "per-tool policy; do not assume preapproval",
        network_authorization=("network commands and dependency installation are launch-authorized"
                               if full else "subject to per-tool policy and approval; not globally preauthorized"),
        inner_sandbox=("disabled; external container remains the boundary"
                       if isinstance(sandbox, ExternalContainerSandbox) else type(sandbox).__name__),
        current_date=(now or datetime.now(timezone.utc)).date().isoformat(),
    )
