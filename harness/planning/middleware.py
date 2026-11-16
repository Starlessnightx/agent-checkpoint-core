from __future__ import annotations

import json

from harness.core.hooks import HookAction, HookDecision


class PlanningMiddleware:
    name = "goal_planning"
    order = 25

    def __init__(self, store, background):
        self.store, self.background = store, background

    def checkpoint_state(self):
        return {"schema_version": 1, "plan_root": str(self.store.root),
                "shared_background": True}

    def restore_checkpoint_state(self, value):
        from harness.core.portable_state import copy_plain, require_fields
        value = copy_plain(value)
        require_fields(value, {'schema_version', 'plan_root', 'shared_background'})
        if (type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['shared_background'] is not True
                or value['plan_root'] not in (str(self.store.root), str(self.store.checkpoint_origin_root))):
            raise ValueError('planning middleware binding differs')

    def handle(self, context):
        session = context.session
        if context.event == "AfterModel" and session.model_context is not None:
            self.store.evidence.presented(json.dumps(session.model_context.messages, ensure_ascii=False))
        if context.event in {"AfterTool", "ToolFailure"}:
            result = context.payload.get("result")
            data = result.structured or {} if result else {}
            if data.get("agent_id"):
                task = self.store.task(data["plan_task_id"]) if data.get("plan_task_id") else {}
                self.background.watch_child(data["agent_id"], task.get("estimated_duration_seconds"))
            if result and result.state_delta:
                for handle in result.state_delta.processes_started:
                    self.background.watch_process(handle)
            if "plan_updated" in data or "plan_snapshot" in data:
                session.emit("plan_updated" if "plan_updated" in data else "plan_read",
                             plan=data.get("plan_updated", data.get("plan_snapshot")),
                             metadata_only=True)
            for notice in data.get("task_notifications", []):
                session.emit("task_notification", **notice)
            if "wait_outcome" in data:
                session.emit("background_review", **data)
            if "process_wait" in data:
                session.emit("background_review", **data)
            if "evidence_inspected" in data:
                session.emit("plan_evidence_inspected", receipt=data["evidence_inspected"], metadata_only=True)
            if "subagent_collected" in data:
                session.emit("plan_child_collected", agent_id=data["subagent_collected"])
            return None
        if context.event == "BeforeModel":
            notices = self.background.drain()
            for notice in notices:
                session.emit("task_notification", **notice)
            value = self.store.projection()
            for task in value["tasks"]:
                if task.get("process_handle"):
                    self.background.watch_process(task["process_handle"], task.get("estimated_duration_seconds"))
            # Replace the transient view instead of appending a full plan each turn.
            session.messages[:] = [message for message in session.messages
                                   if not (message.get("role") == "system" and str(message.get("content", "")).startswith("Current plan metadata (claims require verification):\n"))]
            if value["version"] or notices:
                session.messages.append({
                    "role": "system",
                    "content": "Current plan metadata (claims require verification):\n"
                    + json.dumps(value, ensure_ascii=False)
                    + "\nCompletion notifications:\n" + json.dumps(notices + [
                        notice for notice in self.background.records
                        if notice.get("agent_id") in {task.get("owner") for task in value["tasks"]}
                        and notice not in notices
                    ], ensure_ascii=False)
                    + "\nChoose useful ready work subject to resources. Accept child evidence before completing tasks."
                })
        if context.event == "BeforeVerification":
            unfinished = [task["id"] for task in self.store.snapshot()["tasks"]
                          if task["status"] not in {"completed", "cancelled"}]
            if unfinished or self.background.children_active():
                return HookDecision(HookAction.BLOCK, reason="planned work or background tasks remain unresolved",
                                    value="Before finishing, resolve planned work or record why it is superseded; "
                                    "review active background tasks and actual verification evidence.")
            for task in self.store.snapshot()["tasks"]:
                if task["status"] == "completed":
                    try:
                        self.store.evidence.validate(task)
                    except (ValueError, OSError) as exc:
                        return HookDecision(HookAction.BLOCK, reason=f"plan evidence invalid for {task['id']}: {exc}",
                                            value="Reinspect evidence changed since acceptance before delivering the result.")
        return None
