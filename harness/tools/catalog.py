"""Builtin capability inventory and a deliberately small model-facing core."""
import os
from .providers import filesystem, shell, project_testing, trajectory_debugger

CORE_TOOLS = frozenset({"read_file", "list_directory", "search_text", "write_file",
                        "apply_patch", "run_command", "start_process", "wait_process"})
CORE_TOOLS = CORE_TOOLS | {"exec_command", "write_stdin"}


def builtin_tool_specs(allowed_groups=None):
    providers = (("filesystem", filesystem), ("shell", shell), ("project-testing", project_testing),
                 ("trajectory-debugger", trajectory_debugger))
    result = tuple(spec for name, provider in providers
                 if allowed_groups is None or name in allowed_groups for spec in provider.tool_specs())
    from .unified_exec import tool_specs
    from .turn_plan import tool_spec

    plan = (tool_spec(),) if allowed_groups is None and os.getenv("HARNESS_ENABLE_TURN_PLAN", "0") == "1" else ()
    return result + (tool_specs() if allowed_groups is None or "shell" in allowed_groups else ()) + plan
