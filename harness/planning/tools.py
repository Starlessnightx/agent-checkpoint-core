from __future__ import annotations

import json

from harness.tools.base import ToolPermission, ToolResult, ToolSpec, object_schema
from .store import PlanStore


def planning_tools(store: PlanStore, coordinator=None) -> tuple[ToolSpec, ...]:
    string = {"type": "string"}
    strings = {"type": "array", "items": string}
    task = object_schema({
        **{key: string for key in ("id", "title", "description", "owner", "acceptance",
                                  "hypothesis", "decision_rule", "process_handle", "next_action")},
        "status": {"type": "string", "enum": ["pending", "running", "blocked", "completed", "failed", "cancelled"]},
        **{key: strings for key in ("dependencies", "evidence_refs", "uncertainties", "resources")},
        "accepted_result": {"type": "boolean"},
        "failure_count": {"type": "integer", "minimum": 0},
        "estimated_duration_seconds": {"type": "number", "exclusiveMinimum": 0},
        "source_refs": strings,
        "experiment_id": string,
        "metric_checks": {"type": "array", "items": object_schema({
            "ref": string, "key": string, "operator": {"type": "string", "enum": [">=", "<=", ">", "<", "=="]},
            "threshold": {"type": "number"}}, ("ref", "key", "operator", "threshold"))},
        "evidence_review": object_schema({"receipt_id": string, "assessment": string,
                                          "outcome": {"type": "string", "enum": ["supported", "refuted", "inconclusive"]}},
                                         ("receipt_id", "assessment", "outcome")),
        "delegation": object_schema({
            **{key: string for key in ("role", "trigger", "scope", "deliverable")},
            "input_refs": strings,
        }),
    }, ("id",))

    def update(context, args):
        try:
            for change in args["tasks"]:
                if change.get("status") == "completed":
                    previous = next((t for t in store.snapshot()["tasks"] if t["id"] == change["id"]), {})
                    owner = previous.get("delegated_agent_id") or change.get("owner", previous.get("owner", "parent"))
                    if owner not in {"", "parent"} and coordinator is not None:
                        result = coordinator.collect(owner)
                        if result is None or result.status != "completed":
                            raise ValueError("delegated work has no successful result to accept")
            value = store.update(args["tasks"], expected_version=args["expected_version"],
                                 reason=args["reason"], success_conditions=args.get("success_conditions"))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return ToolResult(False, str(exc), failure_kind="invalid_plan")
        return ToolResult(True, f"Plan saved at version {value['version']}",
                          structured={"plan_updated": value})

    def read(context, args):
        value = store.snapshot()
        return ToolResult(True, "Plan metadata; completion still requires actual verification",
                          stdout=json.dumps(value, ensure_ascii=False),
                          structured={"plan_snapshot": value})

    def restore(context, args):
        try:
            value = store.restore(args["version"], args.get("session_id"))
        except (ValueError, OSError) as exc:
            return ToolResult(False, str(exc), failure_kind="invalid_plan")
        return ToolResult(True, "Restored plan as a new version", structured={"plan_updated": value})

    def inspect(context, args):
        try:
            value = store.evidence.inspect(store.task(args["task_id"]))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            return ToolResult(False, str(exc), failure_kind="invalid_evidence")
        return ToolResult(True, "Read evidence receipt; assess the returned content in a separate update_plan call. Negative experiments may be completed as refuted/inconclusive.",
                          stdout=json.dumps(value, ensure_ascii=False), structured={"evidence_inspected": value})

    return (
        ToolSpec("inspect_plan_evidence", "Read real evidence and source/config files, hash full bytes and check declared JSON metrics/provenance. Return a receipt for subsequent explicit parent assessment; metadata does not prove task success.",
                 object_schema({"task_id": string}, ("task_id",)), inspect, "planning:evidence",
                 mutates_workspace=True, required_permission=ToolPermission.WORKSPACE_WRITE),
        ToolSpec("update_plan", "Persist or revise a complex task plan. Record hypotheses, dependencies, evidence and conditional delegation; metadata is not the deliverable.",
                 object_schema({"expected_version": {"type": "integer", "minimum": 0},
                                "reason": string, "success_conditions": strings,
                                "tasks": {"type": "array", "items": task}},
                               ("expected_version", "reason", "tasks")), update, "planning:write",
                 mutates_workspace=True, required_permission=ToolPermission.WORKSPACE_WRITE),
        ToolSpec("read_plan", "Read the full current plan. Ready means dependencies met, not resource availability.",
                 object_schema({}), read, "planning:read"),
        ToolSpec("restore_plan", "Restore saved planning metadata, optionally from a previous session with the same objective. Old handles require reconciliation; audit versions remain immutable.",
                 object_schema({"version": {"type": "integer", "minimum": 1}, "session_id": string}, ("version",)), restore,
                 "planning:write", mutates_workspace=True),
    )
