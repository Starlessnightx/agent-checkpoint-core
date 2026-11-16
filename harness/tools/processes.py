from __future__ import annotations

from typing import Any
import time

from .base import StateDelta, ToolContext, ToolResult
from .filesystem import resolve_workspace
from harness.executor.waiting import wait_for_process
from .artifacts import bounded_output
from harness.planning.supervision import ReviewPolicy


def start_process(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    command = str(args.get("command", "")).strip()
    if not command:
        return ToolResult(False, "command must be non-empty", failure_kind="invalid_input")
    try:
        cwd = resolve_workspace(context, str(args.get("cwd", ".")))
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    executor = context.executor
    assert executor is not None
    status = executor.start_process(
        command, cwd, context.artifact_root / "processes"
    )
    context.process_started_times[status.handle] = time.monotonic()
    return ToolResult(
        True,
        f"started process handle={status.handle} pid={status.pid}",
        stdout="Do independent work now. When nothing else is ready, wait_process returns at a review checkpoint or completion; review progress on each return.",
        artifacts=(context.relative(status.log_path),),
        state_delta=StateDelta(processes_started=(status.handle,)),
    )


def process_status(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    handle = str(args.get("handle", ""))
    executor = context.executor
    assert executor is not None
    status = executor.process_status(handle)
    if not status:
        return ToolResult(
            False, f"unknown process handle: {handle}", failure_kind="not_found"
        )
    latest = "\n".join(status.output.splitlines()[-120:])
    return ToolResult(
        True,
        "process is running" if status.running else f"process exited with {status.return_code}",
        stdout=latest,
        exit_code=status.return_code,
        artifacts=(context.relative(status.log_path),),
    )


def stop_process(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    handle = str(args.get("handle", ""))
    executor = context.executor
    assert executor is not None
    status = executor.stop_process(handle)
    if not status:
        return ToolResult(
            False, f"unknown process handle: {handle}", failure_kind="not_found"
        )
    return ToolResult(
        True,
        f"stopped process handle={handle} exit_code={status.return_code}",
        exit_code=status.return_code,
        state_delta=StateDelta(processes_stopped=(handle,)),
    )


def wait_process(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    handle = str(args.get("handle", ""))
    timeout = args.get("timeout_seconds")
    executor = context.executor
    assert executor is not None
    background = context.background_tasks
    if background is not None:
        background.watch_process(handle, args.get("estimated_duration_seconds"))
        interval = background.checkpoint_seconds(args.get("estimated_duration_seconds"))
    else:
        now = time.monotonic()
        started = context.process_started_times.setdefault(handle, now)
        interval = ReviewPolicy().interval(now - started, args.get("estimated_duration_seconds"))
    if args.get("until_exit"):
        # Explicit blocking wait: skip the review cadence and stay until exit.
        deadline = min(float(timeout) if timeout is not None else 1800.0, 3600.0)
        deadline = max(0.1, deadline)
    else:
        deadline = min(float(timeout), interval) if timeout is not None else interval
    result = wait_for_process(
        executor, handle,
        timeout_seconds=deadline,
        max_poll_interval_seconds=min(30.0, max(1.0, deadline * 0.1)),
        interrupted=context.interrupt_wait,
    )
    status = result.status
    if status is None:
        return ToolResult(False, f"unknown process handle: {handle}",
                          failure_kind="not_found", structured={"process_wait": result.dictionary()})
    output, archived = bounded_output(context, "process-waits", status.output)
    failed = result.outcome == "completed" and status.return_code != 0
    summaries = {
        "completed": f"process completed with exit_code={status.return_code}",
        "timeout": "review checkpoint reached; inspect progress and choose useful work; process remains running (not killed)",
        "interrupted": "wait interrupted by control; process ownership is unchanged",
    }
    return ToolResult(
        not failed, summaries[result.outcome], stdout=output,
        exit_code=status.return_code,
        artifacts=(context.relative(status.log_path), *archived),
        failure_kind="process_exit" if failed else None,
        structured={"process_wait": {**result.dictionary(),
                    "outcome": "checkpoint" if result.outcome == "timeout" else result.outcome,
                    "review_interval_seconds": deadline}},
    )


__all__ = ["process_status", "start_process", "stop_process", "wait_process"]
