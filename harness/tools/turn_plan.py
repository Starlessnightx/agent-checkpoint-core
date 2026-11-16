"""Advisory Codex-style plan; no phases, completion gate or model calls."""
import json

from .base import StateDelta, ToolResult, ToolSpec, object_schema


def update_plan(context, args):
    plan = args.get("plan")
    if not isinstance(plan, list) or not plan:
        return ToolResult(False, "plan must be a nonempty list", failure_kind="invalid_input")
    for item in plan:
        if (not isinstance(item, dict) or not isinstance(item.get("step"), str) or not item["step"].strip()
                or item.get("status") not in {"pending", "in_progress", "completed"}):
            return ToolResult(False, "each step needs text and pending/in_progress/completed status", failure_kind="invalid_input")
    if sum(p["status"] == "in_progress" for p in plan) > 1:
        return ToolResult(False, "at most one step may be in_progress", failure_kind="invalid_input")
    value = {"plan": [{"step": p["step"], "status": p["status"]} for p in plan],
             "explanation": str(args.get("explanation", ""))}
    path = context.artifact_root / "turn-plan.json"
    existed = context.executor.exists(path)
    context.executor.write_file(path, json.dumps(value, ensure_ascii=False, indent=2).encode())
    name = context.relative(path)
    return ToolResult(True, "Advisory plan updated; completed steps do not assert verification passed",
                      stdout=json.dumps(value, ensure_ascii=False), artifacts=(name,),
                      state_delta=StateDelta(modified=(name,) if existed else (), created=() if existed else (name,)),
                      structured={"advisory_plan": value})


def tool_spec():
    item = object_schema({"step": {"type": "string"}, "status": {"type": "string",
                          "enum": ["pending", "in_progress", "completed"]}}, ("step", "status"))
    return ToolSpec("update_plan", "Save/replace a lightweight task plan; at most one in_progress step. Optional, advisory, no verification gate. Use for multi-step work and revise as evidence changes.",
                    object_schema({"plan": {"type": "array", "items": item, "minItems": 1},
                                   "explanation": {"type": "string"}}, ("plan",)),
                    update_plan, "planning:write", mutates_workspace=True)
