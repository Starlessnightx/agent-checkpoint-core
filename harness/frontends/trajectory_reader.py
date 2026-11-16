"""Read-only projection of provider audit objects for a local trajectory viewer."""
from __future__ import annotations

import json
from pathlib import Path
import re
from threading import RLock
from typing import Any

from harness.api_audit import load_payload


def display_safe(value: Any) -> Any:
    """Keep public model/tool content; do not expose credentials or private reasoning."""
    if isinstance(value, dict):
        if value.get("type") == "reasoning":
            return {"type": "reasoning", "display": "内部推理不展示"}
        return {key: ("[REDACTED]" if key.lower() in {
                    "authorization", "api_key", "access_token", "refresh_token"} else display_safe(item))
                for key, item in value.items()
                if key not in {"reasoning_content", "encrypted_content"}}
    if isinstance(value, list):
        return [display_safe(item) for item in value]
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        return re.sub(r"(?i)Bearer\s+[^\s\"']+", "Bearer [REDACTED]", value)
    return value


class TrajectoryReader:
    def __init__(self, trajectory: Path):
        self.trajectory = trajectory.resolve()
        self.mirror = None
        self._lock = RLock()
        self._signature = None
        self._events = []

    def events(self):
        with self._lock:
            try:
                stat = self.trajectory.stat()
                signature = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            except FileNotFoundError:
                signature = None
            if signature == self._signature:
                return self._events
            events = self._read_events()
            self._signature, self._events = signature, events
            return events

    def _read_events(self):
        events = []
        if not self.trajectory.is_file():
            return events
        with self.trajectory.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue  # A live writer may not have finished its last line.
                if isinstance(event, dict):
                    events.append(event)
        return events

    def summary(self):
        events = self.events()
        responses = {e.get("request_id"): e for e in events if e.get("type") in {"api_response", "api_error"}}
        requests = [e for e in events if e.get("type") == "api_request"]
        sample_counts = {}
        for request in requests:
            sample = request.get("sampling_index")
            sample_counts[sample] = sample_counts.get(sample, 0) + 1
        usages = {e.get("sampling_index"): e for e in events if e.get("type") == "usage_recorded"}
        usage = next((e.get("task_usage", {}) for e in reversed(events)
                      if e.get("type") in {"usage_recorded", "run_end"}), {})
        start = next((e for e in events if e.get("type") == "run_start"), {})
        end = next((e for e in reversed(events) if e.get("type") == "run_end"), {})
        reward_path = self.trajectory.parent.parent / "verifier" / "reward.txt"
        reward = reward_path.read_text().strip() if self.trajectory.parent.name == "agent" and reward_path.is_file() else None
        return display_safe({
            "source": str(self.trajectory), "status": end.get("termination", "running" if events else "waiting"),
            "reward": reward, "mirror": self.mirror.state if self.mirror is not None else None,
            "model": next((e.get("provider_model") for e in requests), None),
            "task": start.get("task", ""), "usage": usage, "event_count": len(events),
            "last_event": events[-1] if events else None,
            "rounds": [{"id": e["request_id"], "index": index + 1,
                        "sampling_index": e.get("sampling_index"), "attempt": e.get("attempt"),
                        "time": e.get("time"), "status": responses.get(e["request_id"], {}).get("type", "pending"),
                        "duration_seconds": responses.get(e["request_id"], {}).get("duration_seconds"),
                        "usage": usages.get(e.get("sampling_index"), {}).get("request_usage") if sample_counts[e.get("sampling_index")] == 1 else None,
                        "cost": usages.get(e.get("sampling_index"), {}).get("request_cost_usd") if sample_counts[e.get("sampling_index")] == 1 else None}
                       for index, e in enumerate(requests)],
        })

    def detail(self, request_id: str):
        events = self.events()
        request = next((e for e in events if e.get("type") == "api_request" and e.get("request_id") == request_id), None)
        if request is None:
            raise KeyError("unknown request")
        if self.mirror is not None:
            return display_safe(self.mirror.read_detail(request_id, full=True))
        response = next((e for e in events if e.get("type") in {"api_response", "api_error"}
                         and e.get("request_id") == request_id), None)

        def body(event):
            if event is None:
                return None
            reference = event.get("payload_ref", "")
            if not re.fullmatch(r"api/objects/[0-9a-f]{64}\.json\.gz", reference):
                raise ValueError("invalid audit reference")
            return load_payload(self.trajectory.parent / "api", reference)

        sample = request.get("sampling_index")
        tool_events = [e for e in events if e.get("sampling_index") == sample and e.get("type") in {
            "tool", "permission_decision", "tool_started", "tool_finished", "tool_failed"}]
        return display_safe({"request_event": request, "request": body(request),
                             "response_event": response, "response": body(response), "tools": tool_events})

    def step(self, request_id: str):
        """Return a compact display projection without loading cumulative request bodies."""
        events = self.events()
        request = next((e for e in events if e.get("type") == "api_request" and e.get("request_id") == request_id), None)
        if request is None:
            raise KeyError("unknown request")
        if self.mirror is not None:
            remote = self.mirror.read_detail(request_id)
            return self._step_projection(remote.get("response"), remote.get("tools", []))
        response = next((e for e in events if e.get("type") in {"api_response", "api_error"} and e.get("request_id") == request_id), None)
        payload = None
        if response is not None:
            reference = response.get("payload_ref", "")
            if not re.fullmatch(r"api/objects/[0-9a-f]{64}\.json\.gz", reference):
                raise ValueError("invalid audit reference")
            payload = load_payload(self.trajectory.parent / "api", reference)
        sample = request.get("sampling_index")
        tool_events = [e for e in events if e.get("sampling_index") == sample and e.get("type") in {
            "tool", "permission_decision", "tool_started", "tool_finished", "tool_failed"}]
        return self._step_projection(payload, tool_events)

    @staticmethod
    def _step_projection(payload, tool_events):
        summaries = []
        if isinstance(payload, dict):
            for item in payload.get("output", []):
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    summaries.extend(part["text"] for part in item.get("summary", [])
                                     if isinstance(part, dict) and part.get("type") == "summary_text"
                                     and isinstance(part.get("text"), str) and part["text"].strip())
            payload = {key: payload[key] for key in ("choices", "output", "error") if key in payload}
        return display_safe({"response": payload, "tools": tool_events, "reasoning_summary": summaries})
