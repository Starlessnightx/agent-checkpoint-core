"""Read-only behavior metrics derived from the raw event stream."""
from typing import Any


def behavior_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    tools = [event for event in events if event.get("type") == "tool"]
    models = [event for event in events if event.get("type") == "model"]
    writes = [event for event in tools if event.get("material_write")]
    return {
        "model_turns": len(models),
        "tool_calls": len(tools),
        "tool_failures": sum(event.get("ok") is False for event in tools),
        "material_writes": len(writes),
        "changed_paths": sorted({str(path) for event in writes for path in event.get("changed_paths", [])}),
        "verification_actions": sum(bool(event.get("verification")) for event in tools),
        "empty_model_responses": sum(bool(event.get("content_empty")) for event in models),
        "retried_model_responses": sum(event.get("type") == "model_response_retry" for event in events),
        "compactions": sum(event.get("type") in {"compaction", "context_compaction"} for event in events),
    }
