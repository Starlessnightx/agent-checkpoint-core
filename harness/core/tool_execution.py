"""Ordered tool-batch execution; permissions and sandbox remain in Registry."""
from __future__ import annotations
import json
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from typing import Any, Callable
from harness.permissions import PermissionLevel
from harness.runtime.recovery import RecoveryGuard
from harness.tools.base import ToolFailure, ToolIntent, ToolRequest
from harness.tools.registry import ToolRegistry
from harness.types import ToolResult
from .hooks import HookContext
from .sampling_support import _inject_hook_context
from .session import Session

PARALLEL_SAFE_TOOLS = frozenset({
    "read_file", "list_directory", "search_text", "process_status", "read_observation",
})

def _prefetch_parallel_results(
    session: Session, calls: list[dict[str, Any]],
    cancelled: Callable[[], bool] | None,
) -> dict[int, ToolResult]:
    """Run independent read-only calls concurrently before the ordered result loop.

    Hooks, approvals, recovery accounting and event/message ordering stay in
    the sequential loop. A prefetched result is used only when the evaluated
    effective call is byte-for-byte the original call. Anything ambiguous
    falls back to the ordinary sequential execution path.
    """
    if len(calls) < 2 or (cancelled is not None and cancelled()):
        return {}
    # Never run a tool before an approval/modification/blocking hook. Only
    # middleware explicitly known not to handle BeforeTool may be bypassed
    # by this optimization; unknown extensions fall back to serial dispatch.
    hooks = getattr(session, "hooks", None)
    if hooks is not None and any(not getattr(h, "parallel_prefetch_safe", False)
                                 for h in hooks.middleware):
        return {}
    budget = getattr(session, "tool_call_budget", None)
    if budget is not None and sum(e.get("type") == "tool" for e in session.events) + len(calls) > budget:
        return {}
    recovery = getattr(session, "recovery", None)
    if recovery is not None and (recovery.signatures or recovery.consecutive_failures):
        return {}
    for call in calls:
        name = str((call.get("function") or {}).get("name", ""))
        if name not in PARALLEL_SAFE_TOOLS:
            return {}
        if hasattr(session.tools, "get"):
            try:
                spec = session.tools.get(name)
            except KeyError:
                return {}
            if spec.side_effects or spec.required_permission is not PermissionLevel.READ_ONLY:
                return {}
        try:
            arguments = json.loads((call.get("function") or {}).get("arguments") or "{}")
        except (json.JSONDecodeError, TypeError):
            return {}
        if not isinstance(arguments, dict):
            return {}

    def dispatch(call: dict[str, Any]) -> ToolResult:
        function = call.get("function") or {}
        return session.tools.dispatch(ToolRequest(
            name=str(function.get("name", "")),
            arguments=json.loads(function.get("arguments") or "{}"),
            call_id=str(call.get("id", "")),
            intent=ToolIntent.NORMAL,
        ))

    try:
        pool = ThreadPoolExecutor(max_workers=min(4, len(calls)))
    except Exception:
        return {}  # No calls were submitted, so serial execution is still safe.
    results: dict[int, ToolResult] = {}
    with pool:
        futures = []
        for call in calls:
            try:
                futures.append((call, pool.submit(dispatch, call)))
            except Exception as exc:
                results[id(call)] = _prefetch_failure(exc, execution_status="not_started")
        for call, future in futures:
            try:
                results[id(call)] = future.result()
            except Exception as exc:
                results[id(call)] = _prefetch_failure(exc, execution_status="unknown")
    return results


def _prefetch_failure(exc: Exception, *, execution_status: str) -> ToolResult:
    details = {"exception_type": type(exc).__name__, "execution_status": execution_status,
               "retry_automatically": False}
    reason = f"Parallel tool dispatch failed: {type(exc).__name__}: {exc}"
    return ToolResult(False, reason, failure=ToolFailure("tool_execution_error", reason, details=details),
                      structured=details)

def execute_tools(
    session: Session, calls: list[dict[str, Any]], turn: int,
    cancelled: Callable[[], bool] | None = None,
) -> None:
    orchestration = session.orchestrator
    deferred_hook_messages: list[dict[str, Any]] = []
    prefetched = _prefetch_parallel_results(session, calls, cancelled)
    for call in calls:
        interrupted = cancelled is not None and cancelled()
        budget_reached = (
            session.tool_call_budget is not None
            and sum(event.get("type") == "tool" for event in session.events)
            >= session.tool_call_budget
        )
        if interrupted or budget_reached:
            # Retain API pairing without executing remaining side effects.
            session.messages.append({
                "role": "tool", "tool_call_id": call.get("id", ""),
                "content": json.dumps({"ok": False, "failure_kind": "cancelled" if interrupted else "budget_exhausted",
                                       "executed": False}),
            })
            session.emit("tool_cancelled" if interrupted else "tool_budget_skipped", turn=turn,
                         tool_call_id=call.get("id", ""),
                         tool_name=(call.get("function") or {}).get("name", ""),
                         executed=False)
            continue
        phase_before = orchestration.phase
        requested_name = str((call.get("function") or {}).get("name", ""))
        before_tool = session.hook(
            "BeforeTool", turn=turn, tool_name=requested_name,
            arguments=_call_arguments(call),
        )
        effective_call = _modified_call(call, before_tool)
        requested_name = str(
            (effective_call.get("function") or {}).get("name", "")
        )
        session.emit(
            "tool_started", turn=turn, phase=phase_before, tool_name=requested_name,
            tool_call_id=str(effective_call.get("id", "")),
        )
        while (before_tool is not None and before_tool.stopped
                and before_tool.data.get("approval")
                and session.tools.context.interaction is not None):
            approved = session.tools.context.interaction.approve_hook(
                requested_name, _call_arguments(effective_call),
                str(effective_call.get("id", "")), _hook_stop_reason(before_tool),
            )
            if not approved:
                break
            approved_call = deepcopy(effective_call)
            before_tool = session.hooks.resume_after_approval(before_tool)
            effective_call = _modified_call(call, before_tool)
            requested_name = str((effective_call.get("function") or {}).get("name", ""))
            if not before_tool.stopped and effective_call != approved_call:
                reason = "Request changed after approval; review the updated action."
                if not session.tools.context.interaction.approve_hook(
                    requested_name, _call_arguments(effective_call),
                    str(effective_call.get("id", "")), reason,
                ):
                    before_tool.stopped = True
                    before_tool.data.update(approval=True, stop_reason=reason)
                    break
        _inject_hook_context(
            session, before_tool, turn, phase_before,
            deferred_messages=deferred_hook_messages,
        )
        if before_tool is not None and before_tool.stopped:
            execution_blocked = True
            approval = bool(before_tool.data.get("approval"))
            reason = _hook_stop_reason(before_tool)
            result = ToolResult(
                False,
                reason,
                failure=ToolFailure(
                    "permission_approval_required" if approval else "hook_blocked",
                    reason,
                ),
            )
            name = requested_name
            arguments = _call_arguments(effective_call)
        else:
            tool_count = sum(event.get("type") == "tool" for event in session.events)
            prefetched_result = (
                prefetched.pop(id(call), None) if effective_call is call else None
            )
            if prefetched_result is not None:
                function = effective_call.get("function") or {}
                name = str(function.get("name", ""))
                arguments = _call_arguments(effective_call)
                blocked = session.recovery.before(
                    name, arguments, tool_count
                )
                if blocked:
                    result = ToolResult(
                        False,
                        blocked,
                        failure_kind="recovery_guard",
                    )
                    execution_blocked = True
                else:
                    result = prefetched_result
                    execution_blocked = False
            else:
                result, name, arguments, execution_blocked = _execute(
                    effective_call, session.tools, session.recovery,
                    tool_count,
                )
        result_hook = session.hook(
            "AfterTool" if result.ok else "ToolFailure",
            turn=turn,
            tool_name=name,
            arguments=arguments,
            result=result,
            state_delta=(
                result.state_delta.dictionary() if result.state_delta else None
            ),
            failure_kind=result.failure_kind,
            ok=result.ok,
        )
        _inject_hook_context(
            session,
            result_hook,
            turn,
            phase_before,
            deferred_messages=deferred_hook_messages,
        )
        if result_hook is not None:
            modified_result = result_hook.data.get("result")
            if isinstance(modified_result, ToolResult):
                result = modified_result
        session.recovery.after(
            result.ok,
            blocked=execution_blocked,
            retryable=result.retryable,
        )
        event = _tool_event(result, name, arguments, turn, phase_before)
        session.emit("tool", **event)
        changed_paths = session.artifacts.apply(event.get("state_delta"))
        if changed_paths:
            session.emit(
                "artifact_changed", turn=turn, phase=phase_before,
                tool=name, paths=list(changed_paths),
            )
        session.emit(
            "tool_finished" if result.ok else "tool_failed",
            turn=turn, phase=phase_before, tool_name=name, ok=result.ok,
            failure_kind=result.failure_kind, changed_paths=list(changed_paths),
        )
        if (not result.ok and not (cancelled is not None and cancelled())
                and (result.structured or {}).get("retry_automatically") is not False):
            request = ToolRequest(
                name=name,
                arguments=arguments,
                call_id=str(effective_call.get("id", "")),
                intent=(
                    ToolIntent.VERIFY if name == "run_check" else ToolIntent.NORMAL
                ),
            )
            healing = session.environment_repair.attempt(
                request, result, session.tools, session.environment
            )
            for repair_event in healing.events:
                repair_type = str(repair_event.get("type", "repair"))
                session.emit(
                    repair_type,
                    **{
                        key: value for key, value in repair_event.items()
                        if key != "type"
                    },
                    turn=turn,
                    phase=phase_before,
                )
            if healing.result is not None:
                result = healing.result
            if healing.recovered and healing.result is not None:
                rerun_arguments = (
                    healing.rerun_request.arguments
                    if healing.rerun_request is not None else arguments
                )
                rerun_event = _tool_event(
                    healing.result, name, rerun_arguments, turn, phase_before
                )
                session.emit("tool", **rerun_event, repair_rerun=True)
                rerun_paths = session.artifacts.apply(
                    rerun_event.get("state_delta")
                )
                if rerun_paths:
                    session.emit(
                        "artifact_changed", turn=turn, phase=phase_before,
                        tool=name, paths=list(rerun_paths), repair_rerun=True,
                    )
                session.emit(
                    "tool_finished", turn=turn, phase=phase_before,
                    tool_name=name, ok=True, failure_kind=None,
                    changed_paths=list(rerun_paths), repair_rerun=True,
                )
        session.messages.append({
            "role": "tool", "tool_call_id": effective_call.get("id", ""),
            "content": result.output,
        })
    # OpenAI-compatible chat APIs require every Tool result for one
    # assistant message to be contiguous. Middleware context still becomes
    # visible on the next turn, but only after the complete Tool batch.
    session.messages.extend(deferred_hook_messages)


def _execute(
    call: dict[str, Any], tools: ToolRegistry, recovery: RecoveryGuard,
    count: int,
) -> tuple[ToolResult, str, dict[str, Any], bool]:
    function = call.get("function") or {}
    name = str(function.get("name", ""))
    try:
        arguments = json.loads(function.get("arguments") or "{}")
        if not isinstance(arguments, dict):
            raise ValueError("arguments must be an object")
    except (json.JSONDecodeError, ValueError) as exc:
        message = f"invalid arguments: {exc}"
        return ToolResult(
            False, message, failure=ToolFailure("invalid_input", message)
        ), name, {}, False
    blocked = recovery.before(name, arguments, count)
    if blocked:
        result = ToolResult(
            False, blocked,
            failure_kind="recovery_guard",
        )
    else:
        result = tools.dispatch(ToolRequest(
            name=name,
            arguments=arguments,
            call_id=str(call.get("id", "")),
            intent=ToolIntent.VERIFY if name == "run_check" else ToolIntent.NORMAL,
        ))
    return result, name, arguments, bool(blocked)

def _tool_event(
    result: ToolResult, name: str, arguments: dict[str, Any],
    turn: int, phase: str,
) -> dict[str, Any]:
    delta = result.state_delta.dictionary() if result.state_delta else None
    changed = sorted({
        str(path)
        for field in ("created", "modified", "deleted")
        for path in ((delta or {}).get(field) or [])
    })
    return {
        "turn": turn, "phase": phase, "name": name, "arguments": arguments,
        "ok": result.ok, "output": result.output, "exit_code": result.exit_code,
        "failure_kind": result.failure_kind, "retryable": result.retryable,
        "failure": result.failure.dictionary() if result.failure else None,
        "artifacts": list(result.artifacts), "state_delta": delta,
        "artifact_refs": [item.dictionary() for item in result.artifact_refs],
        "verification_evidence": (
            result.verification_evidence.dictionary()
            if result.verification_evidence else None
        ),
        "verification": result.verification, "material_write": bool(changed),
        "changed_paths": changed,
        "structured": result.structured,
        "images": [image.dictionary() for image in result.images],
    }

def _call_arguments(call: dict[str, Any]) -> dict[str, Any]:
    raw = (call.get("function") or {}).get("arguments", {})
    if isinstance(raw, dict):
        return dict(raw)
    try:
        value = json.loads(raw or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return dict(value) if isinstance(value, dict) else {}

def _modified_call(
    call: dict[str, Any], context: HookContext | None
) -> dict[str, Any]:
    if context is None or "request" not in context.data:
        return call
    supplied = context.data["request"]
    if isinstance(supplied, ToolRequest):
        name, arguments = supplied.name, supplied.arguments
    elif isinstance(supplied, dict):
        original = call.get("function") or {}
        name = str(supplied.get("name", original.get("name", "")))
        arguments = supplied.get("arguments", _call_arguments(call))
    else:
        return call
    if not isinstance(arguments, dict):
        return call
    return {
        **call,
        "function": {
            **(call.get("function") or {}),
            "name": name,
            "arguments": json.dumps(arguments),
        },
    }

def _hook_stop_reason(context: HookContext) -> str:
    return str(
        context.data.get("stop_reason")
        or "runtime hook blocked this action"
    )
