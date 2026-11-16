from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
import re


@dataclass
class ContextStore:
    """Bounded structured state used to build a model view from events."""

    task_contract: str = ""
    goal: str = ""
    plan: list[str] = field(default_factory=list)
    planning: dict[str, Any] = field(default_factory=dict)
    facts: list[str] = field(default_factory=list)
    actions: list[dict[str, Any]] = field(default_factory=list)
    artifacts: set[str] = field(default_factory=set)
    failures: list[str] = field(default_factory=list)
    environment: dict[str, Any] = field(default_factory=dict)
    memory: list[str] = field(default_factory=list)
    verification: dict[str, Any] = field(default_factory=dict)
    active_skills: list[str] = field(default_factory=list)
    observation_refs: list[str] = field(default_factory=list)

    def ingest(self, events: list[dict[str, Any]]) -> None:
        for event in events:
            kind = event.get("type")
            if kind == "run_start":
                self.task_contract = str(event.get("task", self.task_contract))
                self.goal = self.task_contract
                self.environment = dict(event.get("environment") or {})
            elif kind == "tool":
                action = {"name": event.get("name", "tool"), "ok": bool(event.get("ok")),
                          "summary": str(event.get("output", ""))[:300]}
                self.actions.append(action)
                self.actions = self.actions[-40:]
                delta = event.get("state_delta") or {}
                for key in ("created", "modified"):
                    self.artifacts.update(str(path) for path in delta.get(key) or [])
                self.artifacts.difference_update(str(path) for path in delta.get("deleted") or [])
                if not event.get("ok"):
                    self.failures.append(str(event.get("output", ""))[:400])
                    self.failures = self.failures[-12:]
                if event.get("name") == "load_skill" and event.get("ok"):
                    skill = str((event.get("arguments") or {}).get("name", ""))
                    if skill and skill not in self.active_skills:
                        self.active_skills.append(skill)
            elif kind == "context_fact":
                value = str(event.get("value", ""))
                if value and value not in self.facts:
                    self.facts.append(value)
                    self.facts = self.facts[-30:]
            elif kind == "phase_change":
                guidance = str(event.get("guidance", ""))
                if guidance:
                    self.plan.append(guidance)
                    self.plan = self.plan[-20:]
            elif kind in {"plan_updated", "plan_restored", "plan_read"}:
                plan = event.get("plan") or {}
                self.planning = dict(plan)
                self.plan = [str({key: task.get(key) for key in
                                  ("id", "title", "status", "dependencies", "owner", "next_action")})
                             for task in plan.get("tasks", [])]
            elif kind == "verification_completed":
                self.verification = {
                    "passed": bool(event.get("passed")),
                    "reasons": list(event.get("reasons") or []),
                }

    def add_observation_ref(self, observation_id: str) -> None:
        if observation_id not in self.observation_refs:
            self.observation_refs.append(observation_id)
            self.observation_refs = self.observation_refs[-40:]

    def snapshot(self) -> dict[str, Any]:
        return {
            "task_contract": self.task_contract, "goal": self.goal,
            "plan": list(self.plan), "facts": list(self.facts),
            "planning": dict(self.planning),
            "actions": list(self.actions), "artifacts": sorted(self.artifacts),
            "failures": list(self.failures), "environment": dict(self.environment),
            "memory": list(self.memory), "verification": dict(self.verification),
            "active_skills": list(self.active_skills),
            "observation_refs": list(self.observation_refs),
        }


class ObservationStore:
    """Persist complete observations and retrieve them by stable content ID."""

    def __init__(self, root: Path, *, fallbacks: tuple[Path, ...] = ()):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.fallbacks = fallbacks

    def inherit_checkpoint_archives(self, archives: tuple[Path, ...]) -> None:
        """Bind verified read-only history while keeping new writes in this store.

        Callers supply deployment-restored paths, never paths read from a saved
        state. Retaining this object also preserves existing tool bindings.
        """
        if (type(archives) is not tuple or not archives or self.fallbacks
                or any(not isinstance(path, Path) for path in archives)):
            raise ValueError('explicit archive paths and a fresh observation store are required')
        resolved = tuple(path.resolve() for path in archives)
        if (len(set(resolved)) != len(resolved) or
                any(self.root.is_relative_to(path) or path.is_relative_to(self.root) for path in resolved)):
            raise ValueError('observation archive paths must be distinct from the writable store')
        self.fallbacks = resolved

    def put(self, kind: str, payload: Any, *, summary: str = "") -> str:
        import hashlib, json
        canonical = json.dumps({"kind": kind, "payload": payload}, ensure_ascii=False,
                               sort_keys=True, separators=(",", ":"), default=str)
        observation_id = "O-" + hashlib.sha256(canonical.encode()).hexdigest()[:20]
        if any((root / f"{observation_id}.json").is_file() for root in self.fallbacks):
            return observation_id
        path = self.root / f"{observation_id}.json"
        if not path.exists():
            path.write_text(json.dumps({"id": observation_id, "kind": kind,
                                        "summary": summary, "payload": payload},
                                       ensure_ascii=False, indent=2, default=str), encoding="utf-8")
        return observation_id

    def get(self, observation_id: str) -> dict[str, Any] | None:
        import json
        if not re.fullmatch(r"O-[0-9a-f]{20}", observation_id):
            return None
        for root in (self.root, *self.fallbacks):
            path = root / f"{observation_id}.json"
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
        return None

    def list(self, kind: str | None = None) -> tuple[dict[str, Any], ...]:
        import json
        values = []
        for path in sorted(self.root.glob("O-*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if kind is None or value.get("kind") == kind:
                values.append(value)
        return tuple(values)
