from __future__ import annotations

import json
import re
import threading
import math
from copy import deepcopy
from pathlib import Path
from typing import Any

from harness.executor.base import Executor
from .evidence import EvidenceReviews


STATUSES = {"pending", "running", "blocked", "completed", "failed", "cancelled"}
FIELDS = {"id", "title", "description", "status", "dependencies", "owner",
          "acceptance", "evidence_refs", "hypothesis", "uncertainties",
          "decision_rule", "resources", "delegation", "process_handle",
          "accepted_result", "failure_count", "next_action", "estimated_duration_seconds",
          "evidence_review", "source_refs", "metric_checks", "experiment_id"}


class PlanStore:
    """Session-scoped, versioned planning metadata; all writes use Executor."""

    def __init__(self, executor: Executor, root: Path, objective: str):
        self.executor, self.root, self.objective = executor, root, objective
        self._lock = threading.RLock()
        self._value: dict[str, Any] = {
            "version": 0, "objective": objective, "success_conditions": [],
            "reason": "", "tasks": [],
        }
        self._serial = max([0] + [int(path.stem[1:]) for path in executor.glob(root, "v*.json")])
        self.evidence = EvidenceReviews(executor, root / "evidence")
        self.checkpoint_origin_root = root

    def restore_checkpoint_state(self, value: dict[str, Any]) -> None:
        """Full data import; partial-plan restore and evidence checks stay intact."""
        from .checkpoint_state import restore_plan
        restore_plan(self, value)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._value)

    def checkpoint_state(self) -> dict[str, Any]:
        with self._lock:
            return {"schema_version": 1, "root": str(self.root), "objective": self.objective,
                    "serial": self._serial, "value": self.snapshot(),
                    "evidence": self.evidence.checkpoint_state()}

    def seed_conditions(self, conditions: list[str]) -> None:
        with self._lock:
            if self._value["version"]:
                raise ValueError("cannot seed an existing plan")
            self._value["success_conditions"] = list(conditions)

    def update(self, changes: list[dict[str, Any]], *, expected_version: int,
               reason: str, success_conditions: list[str] | None = None) -> dict[str, Any]:
        with self._lock:
            if expected_version != self._value["version"]:
                raise ValueError("plan changed; read_plan before updating")
            value = self.snapshot()
            tasks = {task["id"]: task for task in value["tasks"]}
            for change in changes:
                if set(change) - FIELDS:
                    raise ValueError("unknown planning fields")
                identifier = change.get("id")
                if not isinstance(identifier, str) or not identifier.strip():
                    raise ValueError("each task needs a non-empty id")
                existing = tasks.get(identifier, {
                    "id": identifier, "title": "", "status": "pending",
                    "dependencies": [], "owner": "parent", "acceptance": "",
                    "evidence_refs": [], "resources": [], "delegation": {},
                })
                tasks[identifier] = {**existing, **deepcopy(change)}
                if tasks[identifier].get("owner", "parent") not in {"", "parent"}:
                    tasks[identifier].setdefault("delegated_agent_id", tasks[identifier]["owner"])
            value["tasks"] = list(tasks.values())
            value["reason"] = reason
            if success_conditions is not None:
                value["success_conditions"] = list(success_conditions)
            self._validate(value)
            for change in changes:
                task = tasks[change["id"]]
                if task["status"] == "completed":
                    self.evidence.validate(task)
            existing_versions = [int(path.stem[1:]) for path in self.executor.glob(self.root, "v*.json")]
            self._serial = max([self._serial, value["version"]] + existing_versions) + 1
            value["version"] = self._serial
            path = self.root / f"v{value['version']:06d}.json"
            self.executor.write_file(path, json.dumps(value, ensure_ascii=False,
                                                     indent=2).encode())
            self._value = value
            return self.snapshot()

    def restore(self, version: int, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            source = self.root
            if session_id is not None:
                if not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
                    raise ValueError("invalid source session id")
                source = (self.root.parent / session_id).resolve()
                if not source.is_relative_to(self.root.parent.resolve()):
                    raise ValueError("source session escapes planning directory")
            path = source / f"v{version:06d}.json"
            value = json.loads(self.executor.read_file(path).content)
            if value.get("objective") != self.objective:
                raise ValueError("restored plan has a different objective")
            self._validate(value)
            # Restoring metadata does not resurrect a process or child context.
            for task in value["tasks"]:
                if session_id is not None:
                    if task["status"] == "running":
                        task["status"] = "blocked"
                        task["next_action"] = "Reconcile saved work and artifacts before resuming; old handles are not live."
                        task["owner"] = "parent"
                        task.pop("process_handle", None)
                if task["status"] == "completed":
                    try:
                        if session_id is not None:
                            raise ValueError("new session has not reviewed evidence")
                        self.evidence.validate(task)
                    except (ValueError, OSError):
                        task["status"] = "blocked"
                        task["owner"] = "parent"
                        task["accepted_result"] = False
                        task.pop("delegated_agent_id", None)
                        task.pop("evidence_review", None)
                        task["next_action"] = "Reinspect saved evidence and provenance before accepting restored results."
                if session_id is not None:
                    task.pop("delegated_agent_id", None)
            if session_id is not None:
                self.evidence.invalidate()
                # Downstream running work was already blocked above; restored
                # completion claims are historical, not new-session approval.
            unresolved = {task["id"] for task in value["tasks"] if task["status"] != "completed"}
            changed = True
            while changed:
                changed = False
                for task in value["tasks"]:
                    if task["status"] in {"running", "completed"} and unresolved.intersection(task["dependencies"]):
                        task["status"] = "blocked"
                        task.pop("evidence_review", None)
                        task["next_action"] = "Reconcile restored dependencies before resuming or accepting this result."
                        unresolved.add(task["id"])
                        changed = True
            versions = self.executor.glob(self.root, "v*.json")
            self._serial = max([self._serial, version] + [int(p.stem[1:]) for p in versions]) + 1
            value["version"] = self._serial
            value["reason"] = f"Restored version {version}"
            self.executor.write_file(self.root / f"v{self._serial:06d}.json",
                                     json.dumps(value, ensure_ascii=False, indent=2).encode())
            self._value = value
            return self.snapshot()

    def task(self, identifier: str) -> dict[str, Any]:
        for task in self.snapshot()["tasks"]:
            if task["id"] == identifier:
                return task
        raise ValueError(f"unknown plan task: {identifier}")

    def ready(self) -> list[dict[str, Any]]:
        value = self.snapshot()
        completed = {task["id"] for task in value["tasks"] if task["status"] == "completed"}
        return [task for task in value["tasks"] if task["status"] == "pending"
                and set(task["dependencies"]) <= completed
                and task.get("owner", "parent") in {"", "parent"}]

    def check_delegation(self, identifier: str, role: str) -> dict[str, Any]:
        task = self.task(identifier)
        if identifier not in {item["id"] for item in self.ready()}:
            raise ValueError("task is already owned, blocked, or not ready")
        delegation = task.get("delegation") or {}
        if delegation.get("role") != role:
            raise ValueError("role does not match planned delegation")
        for key in ("trigger", "scope", "deliverable"):
            if not delegation.get(key):
                raise ValueError(f"delegation requires {key}")
        if not task.get("acceptance"):
            raise ValueError("delegation requires acceptance criteria")
        for relative in delegation.get("input_refs", []):
            path = (self.executor.workspace / relative).resolve()
            if not path.is_relative_to(self.executor.workspace.resolve()):
                raise ValueError("delegation input escapes workspace")
            if not self.executor.is_file(path):
                raise ValueError(f"delegation input is not ready: {relative}")
        occupied = {resource for item in self.snapshot()["tasks"]
                    if item["status"] == "running" for resource in item.get("resources", [])}
        if occupied.intersection(task.get("resources", [])):
            raise ValueError("exclusive resource is already in use")
        return task

    def available_work(self) -> list[dict[str, Any]]:
        occupied = {resource for task in self.snapshot()["tasks"]
                    if task["status"] == "running" for resource in task.get("resources", [])}
        return [task for task in self.ready() if not occupied.intersection(task.get("resources", []))]

    @staticmethod
    def _validate(value: dict[str, Any]) -> None:
        tasks = {task["id"]: task for task in value["tasks"]}
        if len(tasks) != len(value["tasks"]):
            raise ValueError("duplicate task ids")
        visited: set[str] = set()
        visiting: set[str] = set()

        def visit(identifier: str) -> None:
            if identifier in visiting:
                raise ValueError("plan dependency cycle")
            if identifier in visited:
                return
            if identifier not in tasks:
                raise ValueError(f"unknown dependency: {identifier}")
            visiting.add(identifier)
            task = tasks[identifier]
            for field in ("dependencies", "evidence_refs", "resources", "uncertainties", "source_refs"):
                if field in task and (not isinstance(task[field], list)
                                      or any(not isinstance(item, str) for item in task[field])):
                    raise ValueError(f"{field} must be a list of strings")
            if not isinstance(task.get("owner", "parent"), str):
                raise ValueError("owner must be a string")
            if not isinstance(task.get("delegation", {}), dict):
                raise ValueError("delegation must be an object")
            estimate = task.get("estimated_duration_seconds")
            if estimate is not None and (not isinstance(estimate, (float, int)) or isinstance(estimate, bool)
                                         or not math.isfinite(estimate) or estimate <= 0):
                raise ValueError("estimated duration must be finite and positive")
            if not task.get("title") or task.get("status") not in STATUSES:
                raise ValueError("task requires title and valid status")
            for dependency in task.get("dependencies", []):
                visit(dependency)
            if task["status"] in {"running", "completed"}:
                if any(tasks[d]["status"] != "completed" for d in task["dependencies"]):
                    raise ValueError("task dependencies are incomplete")
            if task["status"] == "completed":
                if not task.get("acceptance") or not task.get("evidence_refs"):
                    raise ValueError("completion requires acceptance criteria and evidence references")
                if (task.get("delegated_agent_id") or task.get("owner", "parent") not in {"", "parent"}) and not task.get("accepted_result"):
                    raise ValueError("parent must accept delegated result before completion")
            visiting.remove(identifier)
            visited.add(identifier)

        for identifier in tasks:
            visit(identifier)

    def projection(self) -> dict[str, Any]:
        return project_plan(self.snapshot())


def project_plan(value: dict[str, Any]) -> dict[str, Any]:
    tasks = value.get("tasks", [])
    completed = {task["id"] for task in tasks if task["status"] == "completed"}
    return {
            "version": value["version"], "success_conditions": value["success_conditions"],
            "ready": [task["id"] for task in tasks if task["status"] == "pending"
                      and set(task["dependencies"]) <= completed and task.get("owner", "parent") in {"", "parent"}],
            "tasks": [{key: task.get(key) for key in (
                "id", "title", "status", "dependencies", "owner", "delegated_agent_id", "hypothesis",
                "uncertainties", "decision_rule", "next_action", "resources",
                "process_handle", "delegation", "acceptance", "evidence_refs",
                "estimated_duration_seconds", "evidence_review", "source_refs", "metric_checks", "experiment_id")}
                for task in tasks if task["status"] not in {"completed", "cancelled"}],
            "resolved": [{"id": task["id"], "title": task["title"], "hypothesis": task.get("hypothesis"),
                          "decision_rule": task.get("decision_rule"), "evidence_refs": task.get("evidence_refs", []),
                          "evidence_review": task.get("evidence_review"), "metric_checks": task.get("metric_checks", [])}
                         for task in tasks if task["status"] == "completed"],
    }
