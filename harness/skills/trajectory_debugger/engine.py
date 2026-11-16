from __future__ import annotations

import json
from collections import Counter
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable


EVENT_TYPES = {
    "tool", "tool_call", "tool_result", "state_delta", "verification",
    "context_compaction", "skill_load", "subagent_start", "subagent_result",
    "completion", "model_response", "plan", "hypothesis", "observation",
}
WRITE_TOOLS = {"write_file", "replace_text", "apply_patch"}


def build_index(workspace: Path, patterns: Iterable[str]) -> dict[str, Any]:
    workspace = workspace.resolve()
    sources = _resolve_sources(workspace, patterns)
    events: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source in sources:
        try:
            roots = _read_records(source)
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            errors.append({"source": str(source.relative_to(workspace)), "error": str(exc)})
            continue
        for root in roots:
            _extract(
                root,
                source=str(source.relative_to(workspace)),
                output=events,
                inherited_task="",
            )
    for position, event in enumerate(events, start=1):
        event["event_id"] = f"E-{position:06d}"
    return {
        "schema_version": 1,
        "workspace": str(workspace),
        "sources": [str(path.relative_to(workspace)) for path in sources],
        "source_errors": errors,
        "events": events,
        "metrics": _metrics(events),
    }


def query_index(
    index: dict[str, Any],
    *,
    text: str = "",
    task: str = "",
    event_type: str = "",
    tool: str = "",
    failure_kind: str = "",
    ok: bool | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    needle = text.casefold()
    result = []
    for event in index.get("events", []):
        if task and task.casefold() not in str(event.get("task", "")).casefold():
            continue
        if event_type and event.get("type") != event_type:
            continue
        if tool and event.get("name") != tool:
            continue
        if failure_kind and event.get("failure_kind") != failure_kind:
            continue
        if ok is not None and event.get("ok") is not ok:
            continue
        if needle and needle not in json.dumps(event, ensure_ascii=False, default=str).casefold():
            continue
        result.append(event)
        if len(result) >= max(1, min(limit, 1000)):
            break
    return result


def failure_report(index: dict[str, Any], task: str = "") -> dict[str, Any]:
    selected = [
        event for event in index.get("events", [])
        if not task or task.casefold() in str(event.get("task", "")).casefold()
    ]
    failures = [event for event in selected if event.get("ok") is False]
    writes = [event for event in selected if _is_write(event)]
    checks = [event for event in selected if _is_verification(event)]
    repeated = _repeated_calls(failures)
    last_write_position = max(
        (_event_position(event) for event in writes), default=-1
    )
    post_write_checks = [
        event for event in checks if _event_position(event) > last_write_position
    ]
    facts = []
    if failures:
        facts.append(f"{len(failures)} failed tool events were observed")
    if repeated:
        facts.append(f"{len(repeated)} failed call signatures repeated")
    if writes and not post_write_checks:
        facts.append("no recognized verification occurred after the last write")
    if not selected:
        facts.append("no indexed events matched the requested task")
    hypotheses = []
    if repeated:
        hypotheses.append({
            "hypothesis": "recovery repeated an unchanged action without acquiring discriminating evidence",
            "evidence": sorted({item for record in repeated for item in record["event_ids"]}),
            "falsification": "inspect intervening observations and verify whether arguments or workspace state materially changed",
        })
    if writes and not post_write_checks:
        hypotheses.append({
            "hypothesis": "completion may rely on stale or absent post-change verification",
            "evidence": [writes[-1]["event_id"]],
            "falsification": "locate an indexed VerificationEvidence event after the cited write",
        })
    if failures and not hypotheses:
        hypotheses.append({
            "hypothesis": "the final failed action may be an unrecovered blocker",
            "evidence": [failures[-1]["event_id"]],
            "falsification": "find a later successful, materially different action that resolves the same contract",
        })
    report = {
        "task_filter": task,
        "event_count": len(selected),
        "facts": facts,
        "failed_events": [_brief(event) for event in failures[-20:]],
        "writes": [_brief(event) for event in writes[-20:]],
        "post_write_verification": [_brief(event) for event in post_write_checks[-20:]],
        "repeated_failures": repeated,
        "hypotheses": hypotheses,
        "evidence_boundary": "Hypotheses are candidates only; validate them against raw sources and a new rollout.",
    }
    report["markdown"] = _report_markdown(report)
    return report


def compare_indexes(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_metrics = left.get("metrics") or _metrics(left.get("events", []))
    right_metrics = right.get("metrics") or _metrics(right.get("events", []))
    numeric = sorted(
        key for key in set(left_metrics) | set(right_metrics)
        if isinstance(left_metrics.get(key, 0), (int, float))
        and isinstance(right_metrics.get(key, 0), (int, float))
    )
    deltas = {
        key: right_metrics.get(key, 0) - left_metrics.get(key, 0)
        for key in numeric
    }
    left_failures = Counter(
        event.get("failure_kind") or "unknown"
        for event in left.get("events", []) if event.get("ok") is False
    )
    right_failures = Counter(
        event.get("failure_kind") or "unknown"
        for event in right.get("events", []) if event.get("ok") is False
    )
    return {
        "left_metrics": left_metrics,
        "right_metrics": right_metrics,
        "deltas": deltas,
        "failure_kind_deltas": {
            key: right_failures[key] - left_failures[key]
            for key in sorted(left_failures.keys() | right_failures.keys())
        },
        "interpretation_boundary": (
            "Metric deltas describe behavioral change; they do not establish that a Harness edit caused it."
        ),
    }


def load_index(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError(f"unsupported trajectory index: {path}")
    if not isinstance(value.get("events"), list):
        raise ValueError(f"trajectory index has no event list: {path}")
    return value


def _resolve_sources(workspace: Path, patterns: Iterable[str]) -> list[Path]:
    result: list[Path] = []
    for supplied in patterns:
        pattern = str(supplied)
        if Path(pattern).is_absolute() or ".." in Path(pattern).parts:
            raise PermissionError(f"unsafe trajectory pattern: {pattern}")
        matches = sorted(workspace.glob(pattern))
        if not matches and (workspace / pattern).is_file():
            matches = [workspace / pattern]
        for path in matches:
            resolved = path.resolve()
            if workspace not in resolved.parents:
                raise PermissionError(f"trajectory source escapes workspace: {pattern}")
            if resolved.is_file() and resolved.suffix in {".json", ".jsonl"} and resolved not in result:
                result.append(resolved)
    if not result:
        raise ValueError("no JSON/JSONL trajectory sources matched")
    return result[:1000]


def _read_records(path: Path) -> list[Any]:
    if path.suffix == ".jsonl":
        return [json.loads(line) for line in path.read_text(errors="replace").splitlines() if line.strip()]
    value = json.loads(path.read_text(errors="replace"))
    return value if isinstance(value, list) else [value]


def _extract(value: Any, *, source: str, output: list[dict[str, Any]], inherited_task: str) -> None:
    if isinstance(value, dict):
        task = str(value.get("task_name") or value.get("task") or inherited_task)
        event_type = str(value.get("type") or "")
        if event_type in EVENT_TYPES or ("name" in value and "ok" in value):
            output.append(_normalize(value, source, task))
        for child in value.values():
            _extract(child, source=source, output=output, inherited_task=task)
    elif isinstance(value, list):
        for child in value:
            _extract(child, source=source, output=output, inherited_task=inherited_task)


def _normalize(event: dict[str, Any], source: str, task: str) -> dict[str, Any]:
    arguments = event.get("arguments") or {}
    output = str(event.get("output") or event.get("summary") or "")
    return {
        "source": source,
        "task": task,
        "type": str(event.get("type") or "tool"),
        "name": str(event.get("name") or ""),
        "ok": event.get("ok"),
        "exit_code": event.get("exit_code"),
        "failure_kind": event.get("failure_kind"),
        "retryable": bool(event.get("retryable", False)),
        "arguments": arguments if isinstance(arguments, dict) else {"raw": str(arguments)},
        "output_preview": output[:1000],
        "artifacts": list(event.get("artifacts") or []),
        "state_delta": event.get("state_delta"),
        "verification": event.get("verification"),
        "fingerprint": sha256(
            json.dumps([event.get("name"), arguments], sort_keys=True, default=str).encode()
        ).hexdigest()[:16],
    }


def _metrics(events: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "event_count": len(events),
        "tool_event_count": sum(event.get("type") == "tool" for event in events),
        "failure_count": sum(event.get("ok") is False for event in events),
        "write_count": sum(_is_write(event) for event in events),
        "verification_count": sum(_is_verification(event) for event in events),
        "skill_load_count": sum(event.get("name") == "load_skill" for event in events),
        "subagent_call_count": sum("subagent" in str(event.get("name", "")) for event in events),
        "context_compaction_count": sum(event.get("type") == "context_compaction" for event in events),
        "tasks": sorted({event.get("task") for event in events if event.get("task")}),
    }


def _is_write(event: dict[str, Any]) -> bool:
    delta = event.get("state_delta") or {}
    return event.get("name") in WRITE_TOOLS or any(delta.get(key) for key in ("created", "modified", "deleted"))


def _is_verification(event: dict[str, Any]) -> bool:
    return event.get("name") == "run_check" or bool(event.get("verification")) or event.get("type") == "verification"


def _event_position(event: dict[str, Any]) -> int:
    try:
        return int(str(event.get("event_id", "E-0")).split("-")[-1])
    except ValueError:
        return 0


def _repeated_calls(failures: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[str]] = {}
    for event in failures:
        grouped.setdefault(str(event.get("fingerprint")), []).append(str(event.get("event_id")))
    return [
        {"fingerprint": key, "count": len(values), "event_ids": values}
        for key, values in grouped.items() if len(values) > 1
    ]


def _brief(event: dict[str, Any]) -> dict[str, Any]:
    return {key: event.get(key) for key in (
        "event_id", "source", "task", "type", "name", "ok", "exit_code",
        "failure_kind", "output_preview", "artifacts",
    )}


def _report_markdown(report: dict[str, Any]) -> str:
    lines = ["# Failure episode report", "", f"Events: {report['event_count']}", "", "## Observed facts"]
    lines.extend(f"- {fact}" for fact in report["facts"] or ["No notable deterministic fact was extracted."])
    lines.extend(["", "## Candidate hypotheses"])
    for item in report["hypotheses"]:
        lines.extend([
            f"- Hypothesis: {item['hypothesis']}",
            f"  - Evidence: {', '.join(item['evidence'])}",
            f"  - Falsification: {item['falsification']}",
        ])
    lines.extend(["", "## Evidence boundary", "", report["evidence_boundary"]])
    return "\n".join(lines) + "\n"
