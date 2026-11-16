from harness.tools.base import ToolSpec, object_schema
from harness.tools.processes import process_status, start_process, stop_process, wait_process
from harness.tools.shell import run_command


def tool_specs() -> tuple[ToolSpec, ...]:
    path = {"type": "string", "description": "Workspace-relative working directory."}
    return (
        ToolSpec("run_command", "Run one bounded foreground shell command and preserve stdout, stderr, duration, exit code, artifacts, classified failure kind, and any workspace StateDelta that must be reviewed.", object_schema({"command": {"type": "string"}, "cwd": path, "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 600}}, ("command",)), run_command, "process:run", timeout_seconds=600),
        ToolSpec("start_process", "Start a managed background command with a stable handle and persistent combined log.", object_schema({"command": {"type": "string"}, "cwd": path}, ("command",)), start_process, "process:start"),
        ToolSpec("process_status", "Nonblocking inspection for readiness or independent work. For completion waiting use wait_process, not repeated status/sleep loops.", object_schema({"handle": {"type": "string"}}, ("handle",)), process_status, "process:inspect"),
        ToolSpec("wait_process", "Wait for a managed process. By default returns at an adaptive review checkpoint so you can do useful work; set until_exit=true to block until the process exits (timeout_seconds caps the wait). Never kills the process.", object_schema({"handle": {"type": "string"}, "timeout_seconds": {"type": "number", "minimum": 0}, "estimated_duration_seconds": {"type": "number", "exclusiveMinimum": 0}, "until_exit": {"type": "boolean"}}, ("handle",)), wait_process, "process:inspect"),
        ToolSpec("stop_process", "Terminate the process group associated with a managed handle and preserve its final log.", object_schema({"handle": {"type": "string"}}, ("handle",)), stop_process, "process:stop"),
    )
