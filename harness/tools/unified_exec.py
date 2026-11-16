"""Non-PTY process sessions over the existing Executor, never direct OS access."""
import math
import shlex
import time

from harness.executor.waiting import wait_for_process
from .artifacts import bounded_output
from .base import StateDelta, ToolResult, ToolSpec, object_schema
from .command_policy import DEFAULT_MAX_SLEEP_SECONDS, long_sleep_seconds
from .filesystem import resolve_workspace


MAX_YIELD_MS = 30_000
MAX_WAIT_MS = 1_800_000


def _seconds(args, default):
    value = float(args.get("yield_time_ms", default))
    if not math.isfinite(value) or not 0 <= value <= MAX_YIELD_MS:
        raise ValueError(f"yield_time_ms must be between 0 and {MAX_YIELD_MS}")
    return value / 1000


def _result(context, status, args, *, started=False):
    if status is None:
        return ToolResult(False, "unknown session_id (not a host PID)", failure_kind="not_found")
    previous = context.exec_output_offsets.get(status.handle, 0)
    fresh = status.output[previous:]
    context.exec_output_offsets[status.handle] = len(status.output)
    limit = int(args.get("max_output_tokens", 4000))
    if limit < 1:
        raise ValueError("max_output_tokens must be positive")
    output, artifacts = bounded_output(context, "exec-output", fresh)
    if len(output) > limit * 4:
        output = output[:limit * 4] + f"\n[output truncated; full log: {context.relative(status.log_path)}]"
    failed = not status.running and status.return_code != 0
    return ToolResult(not failed,
        f"Process running with session ID {status.handle}" if status.running else f"Process exited with code {status.return_code}",
        stdout=output, exit_code=status.return_code,
        artifacts=(context.relative(status.log_path), *artifacts),
        failure_kind="command_failed" if failed else None,
        state_delta=StateDelta(processes_started=(status.handle,) if started else (),
                               processes_stopped=(status.handle,) if not status.running else ()),
        structured={"process_session": {"session_id": status.handle, "running": status.running,
                    "return_code": status.return_code, "output_offset": len(status.output), "pty": False}})


def exec_command(context, args):
    try:
        if not str(args.get("cmd", "")).strip():
            raise ValueError("cmd must not be empty")
        sleep_seconds = long_sleep_seconds(str(args["cmd"]))
        if sleep_seconds is not None:
            return ToolResult(
                False,
                f"long sleep ({sleep_seconds:g}s) exceeds {DEFAULT_MAX_SLEEP_SECONDS:g}s; "
                "use write_stdin or managed wait tools, and inspect process/log progress before waiting again",
                failure_kind="invalid_input",
            )
        background = bool(args.get("background", False))
        # Blocking is the default: the model should see the result before the
        # next tool call. Explicit background=true or yield_time_ms opts out.
        default_wait = not background and "yield_time_ms" not in args
        wait = bool(args.get("wait", default_wait))
        if background and wait:
            raise ValueError("background=true cannot be combined with wait=true")
        if args.get("tty") or (args.get("timeout_ms") is not None and not wait):
            return ToolResult(False, "PTY is unsupported; timeout_ms requires wait=true and a blocking call",
                              failure_kind="unsupported_feature")
        if wait and args.get("timeout_ms") is not None:
            timeout_ms = float(args["timeout_ms"])
            if not math.isfinite(timeout_ms) or not 0 < timeout_ms <= MAX_WAIT_MS:
                raise ValueError(f"timeout_ms must be between 0 and {MAX_WAIT_MS}")
        if args.get("sandbox_permissions", "use_default") != "use_default" or args.get("prefix_rule"):
            return ToolResult(False, "tools cannot expand launch permissions; configure the launcher instead",
                              failure_kind="permission_denied")
        seconds = _seconds(args, 10000)
        if int(args.get("max_output_tokens", 4000)) < 1:
            raise ValueError("max_output_tokens must be positive")
        cwd = resolve_workspace(context, str(args.get("workdir", ".")))
        shell = args.get("shell", "sh")
        if shell not in {"sh", "bash", "/bin/sh", "/bin/bash"}:
            raise ValueError("shell must be sh or bash")
        command = shlex.join([shell, "-lc" if args.get("login", False) else "-c", str(args["cmd"])])
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    except (ValueError, TypeError) as exc:
        return ToolResult(False, str(exc), failure_kind="invalid_input")
    executor = context.executor
    status = executor.start_process(command, cwd, context.artifact_root / "processes",
                                    stdin=getattr(executor, "supports_process_stdin", False))
    context.process_started_times[status.handle] = time.monotonic()
    if wait:
        # Blocking call: stay inside this tool call until the process exits or
        # the caller's timeout expires. No session_id round trips.
        budget = float(args.get("timeout_ms") or MAX_WAIT_MS) / 1000
        deadline = time.monotonic() + budget
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                waited = wait_for_process(executor, status.handle, timeout_seconds=0,
                                          interrupted=context.interrupt_wait)
                return _result(context, waited.status, args, started=True)
            waited = wait_for_process(executor, status.handle, timeout_seconds=remaining,
                                      interrupted=context.interrupt_wait)
            if not waited.status.running:
                return _result(context, waited.status, args, started=True)
            if time.monotonic() >= deadline:
                return _result(context, waited.status, args, started=True)
    waited = wait_for_process(executor, status.handle, timeout_seconds=seconds,
                             interrupted=context.interrupt_wait)
    return _result(context, waited.status, args, started=True)


def write_stdin(context, args):
    try:
        seconds = _seconds(args, 1000)
        if int(args.get("max_output_tokens", 4000)) < 1:
            raise ValueError("max_output_tokens must be positive")
        handle = str(args["session_id"])
        status = context.executor.process_status(handle)
        if status is None:
            return _result(context, None, args)
        chars = args.get("chars", "")
        if not isinstance(chars, str):
            raise ValueError("chars must be a string")
        if chars == "\x03":
            return _result(context, context.executor.stop_process(handle), args)
        if chars:
            context.executor.write_process_input(handle, chars)
        waited = wait_for_process(context.executor, handle, timeout_seconds=seconds,
                                  interrupted=context.interrupt_wait)
        return _result(context, waited.status, args)
    except NotImplementedError as exc:
        return ToolResult(False, str(exc), failure_kind="unsupported_feature")
    except (ValueError, KeyError, TypeError) as exc:
        return ToolResult(False, str(exc), failure_kind="invalid_input")
    except (OSError, TimeoutError) as exc:
        return ToolResult(False, str(exc), failure_kind="stdin_failed")


def tool_specs():
    output = {"type": "integer", "minimum": 1}
    delay = {"type": "number", "minimum": 0, "maximum": MAX_YIELD_MS}
    return (
        ToolSpec("exec_command", "Run sh/bash through Executor. Blocks until the process exits (default, up to 30 minutes) so the result is available before the next action; pass background=true only for genuinely parallel long work, in which case a session_id is returned. yield_time_ms (max 30000 ms) also opts out of blocking. No PTY, no permission escalation. Full output stays in the process log.",
                 object_schema({"cmd": {"type": "string"}, "workdir": {"type": "string"},
                     "yield_time_ms": delay, "max_output_tokens": output,
                     "shell": {"type": "string"}, "login": {"type": "boolean"}, "tty": {"type": "boolean"},
                     "sandbox_permissions": {"type": "string", "enum": ["use_default"]}}, ("cmd",)),
                 exec_command, "process:run", timeout_seconds=40,
                 permission_argument_aliases=(("cmd", "command"),)),
        ToolSpec("write_stdin", "Send input to or inspect an Executor process session; empty chars polls incrementally with short yield_time_ms (max 30000 ms). Ctrl-C stops its process group. Non-PTY pipes only; detached Docker stdin is unsupported. Never accepts arbitrary host PIDs.",
                 object_schema({"session_id": {"type": "string"}, "chars": {"type": "string"},
                                "yield_time_ms": delay, "max_output_tokens": output,
                     "wait": {"type": "boolean"},
                     "background": {"type": "boolean"},
                     "timeout_ms": {"type": "number", "minimum": 1, "maximum": MAX_WAIT_MS}}, ("session_id",)),
                 write_stdin, "process:run", timeout_seconds=40),
    )
