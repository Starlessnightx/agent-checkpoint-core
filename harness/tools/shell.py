from __future__ import annotations

from typing import Any

from harness.executor import Command
from harness.diagnosis import FailureClassifier
from .artifacts import bounded_output
from .base import StateDelta, ToolContext, ToolResult
from .command_policy import DEFAULT_MAX_SLEEP_SECONDS, long_sleep_seconds
from .filesystem import resolve_workspace


def classify_failure(
    exit_code: int | None,
    output: str,
    *,
    timed_out: bool = False,
) -> tuple[str | None, bool]:
    lowered = output.lower()
    if timed_out:
        return "timeout", True
    if "command not found" in lowered or "not found" in lowered and exit_code == 127:
        return "missing_dependency", False
    if "permission denied" in lowered:
        return "permission", False
    if "no space left on device" in lowered:
        return "disk_full", False
    if "out of memory" in lowered or "killed" in lowered and exit_code in {137, -9}:
        return "out_of_memory", True
    if "temporary failure" in lowered or "connection reset" in lowered:
        return "transient_network", True
    if exit_code:
        return "command_failed", False
    return None, False


def run_command(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    command = str(args.get("command", "")).strip()
    if not command:
        return ToolResult(False, "command must be non-empty",
                          failure_kind="invalid_input")
    sleep_seconds = long_sleep_seconds(command)
    if sleep_seconds is not None:
        return ToolResult(
            False,
            f"long sleep ({sleep_seconds:g}s) exceeds {DEFAULT_MAX_SLEEP_SECONDS:g}s; "
            "use write_stdin or managed wait tools, and inspect process/log progress before waiting again",
            failure_kind="invalid_input",
        )
    try:
        cwd = resolve_workspace(context, str(args.get("cwd", ".")))
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    timeout = min(600.0, max(0.1, float(args.get("timeout_seconds", 90))))
    executor = context.executor
    assert executor is not None
    before = executor.snapshot(context.workspace)
    executed = executor.exec(Command(command, cwd, timeout, shell=True))
    stdout, stderr = executed.stdout, executed.stderr
    timed_out = executed.timed_out
    elapsed = executed.duration_seconds
    combined = stdout + (f"\n[stderr]\n{stderr}" if stderr else "")
    bounded, artifacts = bounded_output(context, "commands", combined)
    failure_kind, retryable = classify_failure(
        executed.return_code, combined, timed_out=timed_out
    )
    diagnosis = FailureClassifier().diagnose(
        {"output": combined, "summary": command},
        getattr(context, "environment_state", None),
    ) if failure_kind else None
    after = executor.snapshot(context.workspace)
    delta = StateDelta(
        created=tuple(sorted(after.keys() - before.keys())),
        modified=tuple(sorted(
            path for path in after.keys() & before.keys()
            if after[path] != before[path]
        )),
        deleted=tuple(sorted(before.keys() - after.keys())),
    )
    changed = delta.created or delta.modified or delta.deleted
    summary = (
        f"command {'timed out' if timed_out else 'completed'} in {elapsed:.2f}s"
        + (
            "; command changed the workspace—review StateDelta and clean "
            "temporary artifacts before completion"
            if changed else ""
        )
    )
    return ToolResult(
        not timed_out and executed.return_code == 0,
        summary,
        stdout=bounded,
        exit_code=executed.return_code,
        artifacts=artifacts,
        state_delta=delta if changed else None,
        failure_kind=failure_kind,
        retryable=retryable,
        structured={"failure_diagnosis": diagnosis.dictionary()} if diagnosis else None,
    )
