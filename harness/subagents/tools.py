from __future__ import annotations

import json

from harness.tools.artifacts import bounded_output
from harness.tools.base import ToolContext, ToolResult, ToolSpec, object_schema

from .coordinator import SubagentCoordinator


def subagent_tool_specs(coordinator: SubagentCoordinator) -> tuple[ToolSpec, ...]:
    def list_roles(context: ToolContext, arguments: dict) -> ToolResult:
        return ToolResult(True, json.dumps(coordinator.roles(), ensure_ascii=False, indent=2))

    def spawn(context: ToolContext, arguments: dict) -> ToolResult:
        plan_id = arguments.get("plan_task_id")
        plans = coordinator.plan_store
        planned = None
        if plan_id:
            if plans is None:
                return ToolResult(False, "planning is unavailable", failure_kind="invalid_plan")
            try:
                planned = plans.check_delegation(str(plan_id), str(arguments["role"]))
            except ValueError as exc:
                return ToolResult(False, str(exc), failure_kind="invalid_plan")
        task = str(arguments["task"])
        refs = [str(item) for item in arguments.get("context_refs", [])]
        if planned:
            delegation = planned["delegation"]
            task += (f"\nObjective: {plans.objective}\nPlanned task: {planned['title']}"
                     f"\nScope: {delegation['scope']}\nDeliverable: {delegation['deliverable']}"
                     f"\nAcceptance: {planned['acceptance']}\nHypothesis: {planned.get('hypothesis', '')}"
                     f"\nDecision rule: {planned.get('decision_rule', '')}"
                     "\nReturn evidence, artifacts, uncertainties and verification; do not claim parent acceptance.")
            if planned.get("metric_checks"):
                task += (f"\nExperiment run_id: {planned.get('experiment_id', '')}"
                         f"\nDeclared metric checks: {json.dumps(planned['metric_checks'], ensure_ascii=False)}"
                         f"\nSource/configuration references: {json.dumps(planned.get('source_refs', []), ensure_ascii=False)}"
                         "\nPersist result JSON with run_id, actual metrics and provenance.source_hashes (SHA-256 of source/config files used in this run). Do not relabel another experiment's results.")
            refs = list(dict.fromkeys(refs + delegation.get("input_refs", [])))
        agent_id = coordinator.spawn(
            str(arguments["role"]),
            task,
            refs,
            arguments.get("workspace_mode"),
        )
        structured = {"agent_id": agent_id, "plan_task_id": plan_id}
        if planned:
            try:
                value = plans.update([{"id": plan_id, "owner": agent_id, "status": "running"}],
                                     expected_version=plans.snapshot()["version"], reason="Delegated task started")
            except ValueError:
                coordinator.cancel(agent_id)
                return ToolResult(False, "plan changed during delegation; child cancelled", failure_kind="invalid_plan")
            coordinator.associate_task(agent_id, str(plan_id))
            structured["plan_updated"] = value
        return ToolResult(True, f"Subagent started: {agent_id}; completion will be notified", structured=structured)

    def status(context: ToolContext, arguments: dict) -> ToolResult:
        return ToolResult(True, json.dumps(coordinator.status(str(arguments["agent_id"])), ensure_ascii=False))

    def collect(context: ToolContext, arguments: dict) -> ToolResult:
        result = coordinator.collect(
            str(arguments["agent_id"]), float(arguments.get("timeout_seconds", 0))
        )
        if result is None:
            return ToolResult(True, "Subagent is still running.")
        output, artifacts = bounded_output(
            context,
            "subagents",
            json.dumps(result.bounded_dictionary(), ensure_ascii=False, indent=2),
        )
        if coordinator.plan_store is not None:
            coordinator.plan_store.evidence.collected(str(arguments["agent_id"]))
        return ToolResult(
            result.status == "completed",
            output,
            artifacts=artifacts,
            failure_kind=None if result.status == "completed" else "subagent_failed",
            structured={"subagent_collected": str(arguments["agent_id"])},
        )

    def cancel(context: ToolContext, arguments: dict) -> ToolResult:
        accepted = coordinator.cancel(str(arguments["agent_id"]))
        return ToolResult(True, f"Cancellation requested; accepted={accepted}")

    agent_id = {"type": "string", "description": "Handle returned by spawn_subagent."}
    return (
        ToolSpec("list_subagent_roles", "List subagent roles, optional budgets, and allowed workspace modes.", object_schema({}), list_roles, "subagent:list"),
        ToolSpec("spawn_subagent", "Start a subagent with an independent context. Pass references, not copied parent history.", object_schema({
            "role": {"type": "string", "enum": list(coordinator.specs)},
            "task": {"type": "string"},
            "context_refs": {"type": "array", "items": {"type": "string"}},
            "plan_task_id": {"type": "string", "description": "Ready task with delegation contract in the parent plan."},
            "workspace_mode": {"type": "string", "enum": ["read_only", "shared_write", "patch_only"]},
        }, ("role", "task")), spawn, "subagent:spawn", mutates_workspace=True),
        ToolSpec("subagent_status", "Check a subagent without blocking.", object_schema({"agent_id": agent_id}, ("agent_id",)), status, "subagent:inspect"),
        ToolSpec("collect_subagent_result", "Collect a completed structured result or wait for a bounded duration.", object_schema({"agent_id": agent_id, "timeout_seconds": {"type": "number", "minimum": 0, "maximum": 60}}, ("agent_id",)), collect, "subagent:collect", timeout_seconds=60),
        ToolSpec("cancel_subagent", "Request cancellation of a queued or running subagent.", object_schema({"agent_id": agent_id}, ("agent_id",)), cancel, "subagent:cancel"),
    )
