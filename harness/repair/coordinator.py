from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.diagnosis import FailureCategory, FailureClassifier, FailureDiagnosis
from harness.tools.base import ToolRequest, ToolResult

from .memory import RepairMemory
from .transaction import (
    RepairAction, RepairKind, RepairPolicy, RepairTransaction,
)


SUPPORTED = frozenset({
    FailureCategory.MISSING_BINARY,
    FailureCategory.MISSING_PACKAGE,
    FailureCategory.INTERPRETER_MISMATCH,
    FailureCategory.WRONG_WORKING_DIRECTORY,
})


@dataclass(frozen=True)
class HealingOutcome:
    attempted: bool
    recovered: bool
    result: ToolResult | None
    events: tuple[dict[str, Any], ...]
    rerun_request: ToolRequest | None = None


class EnvironmentSelfHealer:
    """Bounded deterministic ToolFailure repair policy used by the Runtime."""

    def __init__(self, workspace: Path, memory: RepairMemory | None = None):
        self.workspace = workspace.resolve()
        self.memory = memory or RepairMemory(
            self.workspace / ".harness" / "repair-memory"
        )
        self.classifier = FailureClassifier()
        self._attempted: set[str] = set()

    def attempt(
        self,
        request: ToolRequest,
        failure: ToolResult,
        registry: Any,
        environment: Any,
    ) -> HealingOutcome:
        events: list[dict[str, Any]] = []
        diagnosis = self._diagnose(request, failure, registry, environment, events)
        events.append({"type": "environment_diagnosis", **diagnosis.dictionary()})
        signature = f"{request.name}:{diagnosis.signature}"
        if diagnosis.category not in SUPPORTED:
            events.append({
                "type": "repair_skipped", "signature": signature,
                "reason": "unsupported_failure_category",
            })
            return HealingOutcome(False, False, None, tuple(events))
        if signature in self._attempted:
            events.append({
                "type": "repair_skipped", "signature": signature,
                "reason": "bounded_attempt_already_used",
            })
            return HealingOutcome(False, False, None, tuple(events))
        self._attempted.add(signature)

        constraints = _environment_constraints(environment)
        record = self.memory.resolve(signature, constraints)
        policy = record.policy if record else self._plan(diagnosis, request, registry)
        events.append({
            "type": "repair_memory_lookup", "signature": signature,
            "hit": record is not None,
            "policy_id": record.policy_id if record else None,
        })
        if policy is None:
            events.append({
                "type": "repair_skipped", "signature": signature,
                "reason": "no_safe_typed_repair",
            })
            return HealingOutcome(False, False, None, tuple(events))

        rerun_request = _repaired_request(request, policy)
        events.append({
            "type": "repair_planned", "signature": signature,
            "policy": policy.name,
            "actions": [_action_event(action) for action in policy.actions],
            "original_request": _request_event(request),
            "rerun_request": _request_event(rerun_request),
        })
        transaction = RepairTransaction(
            registry.context.executor,
            self.workspace,
            dispatch=registry.execute,
        )
        rerun_result: ToolResult | None = None

        def exact_rerun() -> bool:
            nonlocal rerun_result
            rerun_result = registry.dispatch(rerun_request)
            return rerun_result.ok

        result = transaction.run(
            policy,
            diagnosis.category.value,
            rerun=exact_rerun,
        )
        events.extend(transaction.events)
        events.append({
            "type": "failed_action_rerun",
            "request": _request_event(rerun_request),
            "ok": bool(rerun_result and rerun_result.ok),
            "failure_kind": rerun_result.failure_kind if rerun_result else None,
        })
        if record:
            self.memory.record_result(record.policy_id, result)
            policy_id = record.policy_id
        elif result.status == "committed":
            saved = self.memory.save(signature, policy, constraints)
            self.memory.record_result(saved.policy_id, result)
            policy_id = saved.policy_id
        else:
            policy_id = None
        events.append({
            "type": "repair_memory_update",
            "signature": signature,
            "stored": policy_id is not None and result.status == "committed",
            "policy_id": policy_id,
            "transaction_status": result.status,
        })
        return HealingOutcome(
            True,
            result.status == "committed" and bool(rerun_result and rerun_result.ok),
            rerun_result,
            tuple(events),
            rerun_request,
        )

    def _diagnose(
        self,
        request: ToolRequest,
        failure: ToolResult,
        registry: Any,
        environment: Any,
        events: list[dict[str, Any]],
    ) -> FailureDiagnosis:
        diagnosis = self.classifier.diagnose(
            {"summary": failure.summary, "output": failure.output}, environment
        )
        if diagnosis.category is not FailureCategory.UNKNOWN:
            return diagnosis
        package = _module_name(failure.output)
        if not package or request.name != "run_command":
            return diagnosis
        interpreter = _available_interpreter(registry)
        if not interpreter:
            return diagnosis
        probe = registry.dispatch(ToolRequest(
            "run_command",
            {
                "command": (
                    f"{shlex.quote(interpreter)} -c "
                    + shlex.quote(
                        "import importlib.util,sys;"
                        f"sys.exit(0 if importlib.util.find_spec({package!r}) else 1)"
                    )
                ),
                "cwd": ".",
                "timeout_seconds": 10,
            },
        ))
        events.append({
            "type": "environment_probe", "probe": "python_module",
            "package": package, "ok": probe.ok,
        })
        values = _environment_dictionary(environment)
        values["packages"] = {package: probe.ok}
        return self.classifier.diagnose(failure.output, values)

    def _plan(
        self, diagnosis: FailureDiagnosis, request: ToolRequest, registry: Any
    ) -> RepairPolicy | None:
        command = str(request.arguments.get("command", ""))
        category = diagnosis.category
        if request.name != "run_command":
            return None
        if category is FailureCategory.MISSING_BINARY:
            missing = _command_name(command)
            alternative = _alternative_binary(missing, registry)
            if not alternative:
                return None
            action = RepairAction(
                RepairKind.USE_ALTERNATIVE_CAPABILITY.value,
                {"missing": missing, "alternative": alternative},
                "Reuse an available equivalent executable before considering installation.",
                (f"{missing} is unavailable", f"{alternative} is available"),
                "read_only", (), ("rerun failed tool request",), "none",
            )
        elif category is FailureCategory.MISSING_PACKAGE:
            package = _module_from_diagnosis(diagnosis)
            interpreter = _available_interpreter(registry)
            if not package or not interpreter:
                return None
            action = RepairAction(
                RepairKind.INSTALL_PYTHON_PACKAGE.value,
                {"package": package, "interpreter": interpreter,
                 "target": ".harness-env/python-packages"},
                "Install one diagnosed package into the bounded workspace package directory.",
                ("module absent from selected interpreter", "pip available"),
                "workspace_write", ("network", "workspace"),
                ("rerun exact failed tool request",), "restore workspace snapshot",
            )
        elif category is FailureCategory.INTERPRETER_MISMATCH:
            current = _command_name(command)
            alternative = _alternative_interpreter(current, registry)
            if not alternative:
                return None
            action = RepairAction(
                RepairKind.SELECT_INTERPRETER.value,
                {"current": current, "interpreter": alternative},
                "Select an available compatible interpreter.",
                ("current interpreter mismatch diagnosed",),
                "read_only", (), ("rerun failed tool request",), "none",
            )
        elif category is FailureCategory.WRONG_WORKING_DIRECTORY:
            current = str(request.arguments.get("cwd", "."))
            if current in {"", "."}:
                return None
            action = RepairAction(
                RepairKind.CHANGE_WORKING_DIRECTORY.value,
                {"from": current, "cwd": "."},
                "Retry from the task workspace root.",
                ("failed request used a workspace subdirectory",),
                "read_only", (), ("rerun failed tool request",), "none",
            )
        else:
            return None
        return RepairPolicy(
            f"typed-{category.value}",
            (category.value,),
            action.preconditions,
            (diagnosis.signature,),
            (action,),
            action.verification,
            action.rollback,
        )


def _repaired_request(request: ToolRequest, policy: RepairPolicy) -> ToolRequest:
    arguments = dict(request.arguments)
    for action in policy.actions:
        values = action.arguments
        if action.kind == RepairKind.USE_ALTERNATIVE_CAPABILITY.value:
            arguments["command"] = _replace_command(
                str(arguments.get("command", "")),
                str(values.get("missing", "")),
                str(values.get("alternative", "")),
            )
        elif action.kind == RepairKind.SELECT_INTERPRETER.value:
            arguments["command"] = _replace_command(
                str(arguments.get("command", "")),
                str(values.get("current", "")),
                str(values.get("interpreter", "")),
            )
        elif action.kind == RepairKind.CHANGE_WORKING_DIRECTORY.value:
            arguments["cwd"] = str(values.get("cwd", "."))
        elif action.kind == RepairKind.INSTALL_PYTHON_PACKAGE.value:
            target = str(values.get("target", ".harness-env/python-packages"))
            arguments["command"] = (
                f"PYTHONPATH={shlex.quote(target)} " + str(arguments.get("command", ""))
            )
    return ToolRequest(request.name, arguments, request.call_id, request.intent)


def _replace_command(command: str, old: str, new: str) -> str:
    if not old or not new:
        return command
    return re.sub(rf"^\s*{re.escape(old)}(?=\s|$)", new, command, count=1)


def _command_name(command: str) -> str:
    try:
        return shlex.split(command)[0] if shlex.split(command) else ""
    except ValueError:
        return ""


def _alternative_binary(missing: str, registry: Any) -> str | None:
    executor = registry.context.executor
    if missing == "file":
        executables = {
            name: executor.which(name) for name in ("file", "readelf", "objdump")
        }
        resolution = registry.resolve_capability(
            "binary_metadata", {"executables": executables}, "read_only"
        )
        selected = getattr(resolution, "selected", None)
        if selected is not None and selected.name != missing:
            return str(selected.name)
    candidates = {
        "python": ("python3",),
        "python3": ("python",),
        "pip": ("pip3",),
        "pip3": ("pip",),
    }.get(missing, ())
    return next((name for name in candidates if executor.which(name)), None)


def _alternative_interpreter(current: str, registry: Any) -> str | None:
    candidates = tuple(name for name in ("python3", "python") if name != current)
    executor = registry.context.executor
    return next((name for name in candidates if executor.which(name)), None)


def _available_interpreter(registry: Any) -> str | None:
    executor = registry.context.executor
    return next((name for name in ("python3", "python") if executor.which(name)), None)


def _module_name(output: str) -> str:
    match = re.search(
        r"(?:No module named|ModuleNotFoundError:\s*No module named)\s*['\"]?([A-Za-z0-9_.-]+)",
        output,
        re.IGNORECASE,
    )
    return match.group(1) if match else ""


def _module_from_diagnosis(diagnosis: FailureDiagnosis) -> str:
    for evidence in diagnosis.evidence:
        if evidence.get("source") == "module_name":
            return str(evidence.get("value", ""))
    return ""


def _environment_dictionary(environment: Any) -> dict[str, Any]:
    if isinstance(environment, dict):
        return dict(environment)
    dictionary = getattr(environment, "dictionary", None)
    return dict(dictionary()) if callable(dictionary) else {}


def _environment_constraints(environment: Any) -> dict[str, Any]:
    values = _environment_dictionary(environment)
    return {key: str(values.get(key, "")) for key in ("os", "arch")}


def _action_event(action: RepairAction) -> dict[str, Any]:
    return {
        "kind": action.kind, "arguments": dict(action.arguments),
        "preconditions": list(action.preconditions), "permission": action.permission,
        "side_effects": list(action.side_effects),
        "verification": list(action.verification), "rollback": action.rollback,
    }


def _request_event(request: ToolRequest) -> dict[str, Any]:
    return {"name": request.name, "arguments": dict(request.arguments)}


__all__ = ["EnvironmentSelfHealer", "HealingOutcome"]
