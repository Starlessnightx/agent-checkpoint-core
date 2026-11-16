from harness.tools.base import ToolSpec, object_schema
from harness.tools.repository import git_diff, git_log, git_show, git_status, inspect_repository
from harness.tools.testing import discover_checks, run_check


def tool_specs() -> tuple[ToolSpec, ...]:
    path = {"type": "string", "description": "Workspace-relative project root."}
    return (
        ToolSpec("inspect_repository", "Detect ecosystems, build metadata, entry-point evidence, and candidate checks without recursively reading source.", object_schema({"path": path}), inspect_repository, "repository:inspect"),
        ToolSpec("discover_checks", "Discover test, lint, build, and type-check commands from project-native configuration files.", object_schema({"path": path}), discover_checks, "verification:discover"),
        ToolSpec("run_check", "Execute a named verification target and emit structured command, exit-code, timing, coverage-target, artifact evidence, and workspace side effects that must be cleaned before completion.", object_schema({"command": {"type": "string"}, "target": {"type": "string"}, "cwd": path, "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 600}}, ("command", "target")), run_check, "verification:run", timeout_seconds=600),
        ToolSpec("git_status", "Read concise Git worktree status.", object_schema({"path": path}), git_status, "repository:git"),
        ToolSpec("git_diff", "Read the current Git diff.", object_schema({"path": path}), git_diff, "repository:git"),
        ToolSpec("git_log", "Read recent Git history.", object_schema({"path": path}), git_log, "repository:git"),
        ToolSpec("git_show", "Read the latest Git commit summary.", object_schema({"path": path}), git_show, "repository:git"),
    )
