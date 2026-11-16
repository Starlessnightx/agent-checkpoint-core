from __future__ import annotations

import json

from harness.tools.base import ToolResult, ToolSpec, object_schema

from .store import ObservationStore


def read_observation_spec(store: ObservationStore) -> ToolSpec:
    def read_observation(context, arguments):
        observation_id = str(arguments.get("id", ""))
        value = store.get(observation_id)
        if value is None:
            return ToolResult(
                False,
                f"observation not found: {observation_id}",
                failure_kind="not_found",
            )
        return ToolResult(
            True,
            f"loaded observation {observation_id}",
            stdout=json.dumps(value, ensure_ascii=False, indent=2),
            structured={"observation_id": observation_id},
        )

    return ToolSpec(
        "read_observation",
        "Read the complete payload for an observation reference in the current task.",
        object_schema({"id": {"type": "string"}}, ("id",)),
        read_observation,
        "observation:read",
    )
