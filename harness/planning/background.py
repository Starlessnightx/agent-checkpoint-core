from __future__ import annotations

import time
import math
from threading import RLock
from typing import Any
from .supervision import ReviewPolicy

from harness.tools.base import ToolResult, ToolSpec, object_schema


class BackgroundTasks:
    """Inspect managed work internally; queue completion changes exactly once."""

    def __init__(self, executor, coordinator, *, policy=None, clock=None, sleep=None):
        self.executor, self.coordinator = executor, coordinator
        self.processes: set[str] = set()
        self._delivered: set[str] = set()
        self._pending: list[dict[str, Any]] = []
        self.records: list[dict[str, Any]] = []
        self._lock = RLock()
        self.policy = policy or ReviewPolicy()
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.started = self.clock()
        self.estimates: dict[str, float] = {}
        self.work_started: dict[str, float] = {}
        self._observed: dict[str, dict[str, Any]] = {}
        self._next_inspection: dict[str, float] = {}

    def watch_process(self, handle: str, estimated_seconds: float | None = None) -> None:
        with self._lock:
            self.processes.add(handle)
            self.work_started.setdefault(handle, self.clock())
            if estimated_seconds is not None:
                self.policy.interval(0, estimated_seconds)
                self.estimates[handle] = estimated_seconds

    def checkpoint_state(self, *, processes=None) -> dict[str, Any]:
        from copy import deepcopy
        from dataclasses import asdict
        with self._lock:
            if self.children_active():
                raise RuntimeError("cannot checkpoint active managed background work")
            now = self.clock()
            completed = []
            facts = {status.handle: status for status in (
                self.executor.checkpoint_processes() if processes is None else processes)}
            for handle in sorted(self.processes):
                status = facts.get(handle)
                if status is not None and status.running:
                    raise RuntimeError("managed process is still running")
                if status is not None:
                    completed.append(asdict(status))
            return deepcopy({"schema_version": 1, "processes": completed,
                             "policy": asdict(self.policy),
                             "registered_processes": sorted(self.processes),
                             "delivered": sorted(self._delivered), "pending": self._pending,
                             "records": self.records, "estimates": self.estimates,
                             "elapsed_seconds": now - self.started,
                             "work_elapsed": {key: now - start for key, start in self.work_started.items()},
                             "observed": {key: {**{field: value for field, value in record.items() if field != "last_output_at"},
                                                "last_output_age_seconds": max(0, now - record.get("last_output_at", now))}
                                          for key, record in self._observed.items()},
                             "next_inspection_delays": {key: max(0, at - now) for key, at in self._next_inspection.items()}})

    def restore_idle_checkpoint_state(self, value: dict[str, Any]) -> None:
        """Restore idle scheduling data; process handles require another provider."""
        from dataclasses import asdict
        from harness.core.portable_state import copy_plain, require_fields, strings
        value = copy_plain(value)
        require_fields(value, self.checkpoint_state())
        if (type(value['schema_version']) is not int or value['schema_version'] != 1
                or value['policy'] != asdict(self.policy)):
            raise ValueError('background process history needs a capable Executor importer')
        strings(value['registered_processes'])
        if (len(set(value['registered_processes'])) != len(value['registered_processes'])
                or type(value['processes']) is not list):
            raise ValueError('invalid registered background processes')
        actual = {p.handle: p for p in self.executor.checkpoint_processes()}
        captured = {}
        for record in value['processes']:
            if type(record) is not dict or type(record.get('handle')) is not str or record['handle'] in captured:
                raise ValueError('invalid background process record')
            handle = record['handle']
            restored = actual.get(handle)
            if (restored is None or restored.running or record.get('running') is not False
                    or type(record.get('return_code')) is not int or record['return_code'] != restored.return_code
                    or record.get('command') != restored.command):
                raise ValueError('background process history needs a capable Executor importer')
            captured[handle] = record
        if set(captured) != set(value['registered_processes']):
            raise ValueError('background process coverage differs')
        strings(value['delivered'])
        if len(set(value['delivered'])) != len(value['delivered']):
            raise ValueError('duplicate delivered background record')
        for key in ('pending', 'records'):
            if type(value[key]) is not list or any(type(v) is not dict for v in value[key]):
                raise ValueError('invalid background notification history')
        for key in ('estimates', 'work_elapsed', 'observed', 'next_inspection_delays'):
            if type(value[key]) is not dict:
                raise ValueError('invalid background scheduler state')
        def duration(number):
            if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                raise ValueError('invalid background elapsed time')
            return number
        duration(value['elapsed_seconds'])
        for key in ('estimates', 'work_elapsed', 'next_inspection_delays'):
            for number in value[key].values():
                duration(number)
        observed = {}
        now = self.clock()
        for key, item in value['observed'].items():
            if type(item) is not dict or 'last_output_age_seconds' not in item or 'last_output_at' in item:
                raise ValueError('invalid background output clock')
            age = duration(item['last_output_age_seconds'])
            observed[key] = {k: v for k, v in item.items() if k != 'last_output_age_seconds'}
            observed[key]['last_output_at'] = now-age
        with self._lock:
            if self.processes or self.children_active():
                raise RuntimeError('background restoration requires an idle scheduler')
            self._delivered, self._pending = set(value['delivered']), value['pending']
            self.records, self.estimates = value['records'], value['estimates']
            self.started = now-value['elapsed_seconds']
            self.work_started = {k: now-v for k, v in value['work_elapsed'].items()}
            self._next_inspection = {k: now+v for k, v in value['next_inspection_delays'].items()}
            self._observed = observed
            self.processes = set(value['registered_processes'])

    def checkpoint_seconds(self, estimated_seconds: float | None = None) -> float:
        now = self.clock()
        active = [handle for handle in self.processes if f"process:{handle}" not in self._delivered]
        active += [identifier for identifier in self.coordinator.agent_ids()
                   if self.coordinator.status(identifier).get("done") is False]
        for identifier in active:
            self.work_started.setdefault(identifier, now)
        intervals = [self.policy.interval(now - self.work_started[identifier],
                                         estimated_seconds if estimated_seconds is not None else self.estimates.get(identifier))
                     for identifier in active]
        return min(intervals) if intervals else self.policy.interval(now - self.started, estimated_seconds)

    def watch_child(self, agent_id: str, estimated_seconds: float | None = None) -> None:
        with self._lock:
            self.work_started.setdefault(agent_id, self.clock())
            if estimated_seconds is not None:
                self.policy.interval(0, estimated_seconds)
                self.estimates[agent_id] = estimated_seconds

    def progress(self) -> list[dict[str, Any]]:
        now = self.clock()
        processes = [{**value, "output_quiet_seconds": now - value["last_output_at"]}
                     for value in self._observed.values()]
        children = [{"kind": "subagent", **self.coordinator.status(identifier),
                     "elapsed_seconds": now - self.work_started.get(identifier, now)}
                    for identifier in self.coordinator.agent_ids()]
        return processes + children

    def changed(self) -> bool:
        with self._lock:
            for notice in self.coordinator.take_notifications():
                self._pending.append(notice)
            for handle in sorted(self.processes):
                identifier = f"process:{handle}"
                if identifier in self._delivered:
                    continue
                now = self.clock()
                if now < self._next_inspection.get(handle, 0):
                    continue
                status = self.executor.process_status(handle)
                previous = self._observed.get(handle, {})
                output = status.output[-2000:] if status else ""
                self._observed[handle] = {
                    "handle": handle, "running": bool(status and status.running),
                    "output_tail": output, "last_output_at": now if output != previous.get("output_tail") else previous.get("last_output_at", now),
                    "elapsed_seconds": now - self.work_started[handle],
                }
                inspection = min(30.0, max(0.1, (now - self.work_started[handle]) * 0.03), self.checkpoint_seconds())
                self._next_inspection[handle] = now + inspection
                if status is None or not status.running:
                    self._delivered.add(identifier)
                    self._pending.append({"kind": "process", "handle": handle,
                                          "status": "missing" if status is None else ("completed" if status.return_code == 0 else "failed"),
                                          "exit_code": status.return_code if status else None,
                                          "output_ref": str(status.log_path) if status else None})
            return bool(self._pending)

    def drain(self) -> list[dict[str, Any]]:
        with self._lock:
            self.changed()
            notices, self._pending = self._pending, []
            self.records.extend(notices)
            return notices

    def active(self) -> bool:
        return self.children_active() or any(
            f"process:{handle}" not in self._delivered for handle in self.processes)

    def children_active(self) -> bool:
        return any(self.coordinator.status(identifier).get("done") is False
                   for identifier in self.coordinator.agent_ids())


def waiting_tool(background: BackgroundTasks) -> ToolSpec:
    def wait(context, args):
        started = background.clock()
        timeout = args.get("timeout_seconds")
        if timeout is not None and (not math.isfinite(float(timeout)) or float(timeout) < 0):
            return ToolResult(False, "timeout must be finite and nonnegative", failure_kind="invalid_input")
        try:
            interval = background.checkpoint_seconds(args.get("estimated_duration_seconds"))
        except ValueError as exc:
            return ToolResult(False, str(exc), failure_kind="invalid_input")
        interval = min(interval, float(timeout)) if timeout is not None else interval
        plans = context.plan_store
        ready = plans.available_work() if plans is not None else []
        if ready:
            return ToolResult(True, "Independent ready work exists; continue working rather than waiting",
                              structured={"wait_outcome": "ready", "ready_tasks": [task["id"] for task in ready]})
        delay = 0.1
        while True:
            if context.interrupt_wait and context.interrupt_wait():
                outcome = "interrupted"
                break
            if background.changed():
                outcome = "changed"
                break
            if not background.active():
                outcome = "idle"
                break
            if background.clock() - started >= interval:
                outcome = "checkpoint"
                break
            remaining = min(delay, max(0, interval - (background.clock() - started)))
            background.sleep(remaining)
            delay = min(1.0, delay * 2)
        notices = background.drain()
        if notices:
            outcome = "changed"
        return ToolResult(True, f"Background wait: {outcome}; inspect progress and revise work/next check; processes are not killed",
                          structured={"task_notifications": notices, "wait_outcome": outcome,
                                      "review_interval_seconds": interval, "background_progress": background.progress()})

    return ToolSpec("wait_for_tasks", "Yield to independent ready work, otherwise wait until an adaptive model review checkpoint, completion or user control. Review progress on return, even if still running; no process is killed.",
                    object_schema({"timeout_seconds": {"type": "number", "minimum": 0},
                                   "estimated_duration_seconds": {"type": "number", "exclusiveMinimum": 0}}),
                    wait, "process:inspect")
