from __future__ import annotations

import json
from typing import Any

from harness.skills.trajectory_debugger.engine import (
    build_index, compare_indexes, failure_report, load_index, query_index,
)
from harness.tools.artifacts import bounded_output, store_artifact
from harness.tools.base import ToolContext, ToolResult, ToolSpec, object_schema
from harness.tools.filesystem import resolve_workspace


def _load(context: ToolContext, supplied: str) -> dict[str, Any]:
    return load_index(resolve_workspace(context, supplied))


def _build(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    index = build_index(context.workspace, arguments.get("patterns") or [".harness/trajectory.jsonl"])
    serialized = json.dumps(index, ensure_ascii=False, indent=2)
    artifact = store_artifact(context, "trajectory-index", serialized, ".json")
    return ToolResult(True, f"Indexed {index['metrics']['event_count']} events from {len(index['sources'])} sources. index_path={artifact}", artifacts=(artifact,))


def _query(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    index = _load(context, str(arguments["index_path"]))
    values = query_index(
        index,
        text=str(arguments.get("text", "")),
        task=str(arguments.get("task", "")),
        event_type=str(arguments.get("event_type", "")),
        tool=str(arguments.get("tool", "")),
        failure_kind=str(arguments.get("failure_kind", "")),
        ok=arguments.get("ok"),
        limit=int(arguments.get("limit", 100)),
    )
    output, artifacts = bounded_output(context, "trajectory-query", json.dumps(values, ensure_ascii=False, indent=2))
    return ToolResult(True, f"Matched {len(values)} indexed events.", stdout=output, artifacts=artifacts)


def _report(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    report = failure_report(
        _load(context, str(arguments["index_path"])),
        str(arguments.get("task", "")),
    )
    artifact = store_artifact(context, "trajectory-reports", report["markdown"], ".md")
    output, extra = bounded_output(context, "trajectory-report-json", json.dumps({key: value for key, value in report.items() if key != "markdown"}, ensure_ascii=False, indent=2))
    return ToolResult(True, f"Failure report created. report_path={artifact}", stdout=output, artifacts=(artifact, *extra))


def _compare(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    comparison = compare_indexes(
        _load(context, str(arguments["left_index_path"])),
        _load(context, str(arguments["right_index_path"])),
    )
    output, artifacts = bounded_output(context, "trajectory-comparison", json.dumps(comparison, ensure_ascii=False, indent=2))
    return ToolResult(True, "Compared two trajectory indexes; deltas are descriptive, not causal.", stdout=output, artifacts=artifacts)


def tool_specs() -> tuple[ToolSpec, ...]:
    index_path = {"type": "string", "description": "Workspace-relative index artifact returned by build_trajectory_index."}
    return (
        ToolSpec("build_trajectory_index", "Parse bounded JSON/JSONL trace sources into a source-linked normalized event index and metrics artifact.", object_schema({"patterns": {"type": "array", "items": {"type": "string"}, "minItems": 1}}), _build, "trajectory:index"),
        ToolSpec("query_trajectory_index", "Filter an existing trajectory index by text, task, event type, tool, success, or failure kind.", object_schema({"index_path": index_path, "text": {"type": "string"}, "task": {"type": "string"}, "event_type": {"type": "string"}, "tool": {"type": "string"}, "failure_kind": {"type": "string"}, "ok": {"type": "boolean"}, "limit": {"type": "integer", "minimum": 1, "maximum": 1000}}, ("index_path",)), _query, "trajectory:query"),
        ToolSpec("report_failure_episode", "Create an evidence-cited deterministic failure timeline with clearly labeled falsifiable hypotheses.", object_schema({"index_path": index_path, "task": {"type": "string"}}, ("index_path",)), _report, "trajectory:analyze"),
        ToolSpec("compare_trajectory_runs", "Compare normalized metrics and failure kinds between two indexes without claiming causal attribution.", object_schema({"left_index_path": index_path, "right_index_path": index_path}, ("left_index_path", "right_index_path")), _compare, "trajectory:compare"),
    )
