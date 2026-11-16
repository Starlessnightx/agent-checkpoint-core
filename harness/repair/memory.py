from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable

from .transaction import RepairPolicy, RepairResult, RepairTransaction


@dataclass(frozen=True)
class RepairMemoryRecord:
    policy_id: str
    version: int
    signature: str
    policy: RepairPolicy
    constraints: dict[str, Any]
    enabled: bool = True
    hits: int = 0
    successes: int = 0
    failures: int = 0
    rollbacks: int = 0

    def dictionary(self) -> dict[str, Any]:
        return {
            "policy_id": self.policy_id, "version": self.version,
            "signature": self.signature, "enabled": self.enabled,
            "hits": self.hits, "successes": self.successes,
            "failures": self.failures, "rollbacks": self.rollbacks,
            "constraints": self.constraints,
            "policy": {"name": self.policy.name, "trigger": list(self.policy.trigger),
                       "preconditions": list(self.policy.preconditions),
                       "diagnosis": list(self.policy.diagnosis),
                       "actions": [{
                           "kind": action.kind, "arguments": action.arguments,
                           "description": action.description,
                           "preconditions": list(action.preconditions),
                           "permission": action.permission,
                           "side_effects": list(action.side_effects),
                           "verification": list(action.verification),
                           "rollback": action.rollback,
                       } for action in self.policy.actions],
                       "verification": list(self.policy.verification), "rollback": self.policy.rollback},
        }


class RepairMemory:
    """Versioned, safe, executable repair policy memory."""

    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.records: dict[str, RepairMemoryRecord] = {}
        self.events: list[dict[str, Any]] = []
        self._load()

    def save(self, signature: str, policy: RepairPolicy, constraints: dict[str, Any]) -> RepairMemoryRecord:
        _validate_safe(policy, constraints)
        canonical = json.dumps({"signature": signature, "policy": policy.name,
                                "constraints": constraints}, sort_keys=True, default=str)
        policy_id = "policy-" + hashlib.sha256(canonical.encode()).hexdigest()[:16]
        previous = self.records.get(policy_id)
        record = RepairMemoryRecord(policy_id, (previous.version + 1 if previous else 1),
                                    signature, policy, _safe_copy(constraints),
                                    enabled=True, hits=previous.hits if previous else 0,
                                    successes=previous.successes if previous else 0,
                                    failures=previous.failures if previous else 0,
                                    rollbacks=previous.rollbacks if previous else 0)
        self.records[policy_id] = record
        self._write(record)
        self._event("saved", policy_id=record.policy_id, version=record.version)
        return record

    def resolve(self, signature: str, environment: Any, permission: str = "workspace_write") -> RepairMemoryRecord | None:
        candidates = [record for record in self.records.values()
                      if record.enabled and record.signature == signature and _matches(record.constraints, environment)]
        if not candidates:
            self._event("rejected", signature=signature, reason="no matching enabled policy")
            return None
        selected = sorted(candidates, key=lambda item: (-item.successes, item.failures,
                                                         item.version, item.policy_id))[0]
        self.records[selected.policy_id] = replace(selected, hits=selected.hits + 1)
        self._write(self.records[selected.policy_id])
        self._event("hit", policy_id=selected.policy_id, signature=signature)
        return self.records[selected.policy_id]

    def replay(self, signature: str, environment: Any, transaction: RepairTransaction,
               *, diagnosis: str, probe: Callable[[], bool] | None = None,
               rerun: Callable[[], bool] | None = None,
               regression: Callable[[], bool] | None = None,
               dispatch: Callable[[str, dict[str, Any]], Any] | None = None) -> RepairResult | None:
        record = self.resolve(signature, environment)
        if record is None:
            return None
        result = transaction.run(record.policy, diagnosis, probe=probe, rerun=rerun,
                                 regression=regression, dispatch=dispatch)
        self.record_result(record.policy_id, result)
        return result

    def record_result(self, policy_id: str, result: RepairResult) -> None:
        record = self.records[policy_id]
        committed = result.status == "committed"
        updated = replace(record, successes=record.successes + int(committed),
                          failures=record.failures + int(not committed),
                          rollbacks=record.rollbacks + int(result.status == "rolled_back"),
                          enabled=record.enabled and record.rollbacks + int(result.status == "rolled_back") < 2)
        self.records[policy_id] = updated
        self._write(updated)
        self._event("success" if committed else "rollback", policy_id=policy_id,
                    status=result.status, enabled=updated.enabled)

    def disable(self, policy_id: str, reason: str = "manual") -> None:
        record = self.records[policy_id]
        self.records[policy_id] = replace(record, enabled=False)
        self._write(self.records[policy_id])
        self._event("disabled", policy_id=policy_id, reason=reason)

    def _write(self, record: RepairMemoryRecord) -> None:
        (self.root / f"{record.policy_id}.json").write_text(
            json.dumps(record.dictionary(), ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )

    def _load(self) -> None:
        # Memory is intentionally local and ignores malformed/stale records.
        for path in sorted(self.root.glob("policy-*.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
                policy_value = value["policy"]
                from .transaction import RepairAction
                policy = RepairPolicy(
                    policy_value["name"], tuple(policy_value.get("trigger", ())),
                    tuple(policy_value.get("preconditions", ())), tuple(policy_value.get("diagnosis", ())),
                    tuple(RepairAction(
                        item["kind"], item.get("arguments", {}), item.get("description", ""),
                        tuple(item.get("preconditions", ())),
                        item.get("permission", "read_only"),
                        tuple(item.get("side_effects", ())),
                        tuple(item.get("verification", ())),
                        item.get("rollback", "none"),
                    )
                          for item in policy_value.get("actions", ())),
                    tuple(policy_value.get("verification", ())), policy_value.get("rollback", ""),
                )
                record = RepairMemoryRecord(value["policy_id"], int(value["version"]), value["signature"],
                                            policy, value.get("constraints", {}), bool(value.get("enabled", True)),
                                            int(value.get("hits", 0)), int(value.get("successes", 0)),
                                            int(value.get("failures", 0)), int(value.get("rollbacks", 0)))
                self.records[record.policy_id] = record
            except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
                continue

    def _event(self, event: str, **payload: Any) -> None:
        self.events.append({"type": "repair_memory", "event": event, **payload})


def _matches(constraints: dict[str, Any], environment: Any) -> bool:
    values = environment if isinstance(environment, dict) else getattr(environment, "__dict__", {})
    for key, expected in constraints.items():
        current = values.get(key) if isinstance(values, dict) else None
        if isinstance(expected, (list, tuple, set)):
            if current not in expected:
                return False
        elif current != expected:
            return False
    return True


def _validate_safe(policy: RepairPolicy, constraints: dict[str, Any]) -> None:
    def walk(value: Any, key: str = ""):
        if isinstance(value, dict):
            for child_key, child in value.items():
                if re.search(r"secret|token|password|api[_-]?key", str(child_key), re.I):
                    raise ValueError("repair memory cannot store secret fields")
                walk(child, str(child_key))
        elif isinstance(value, (list, tuple)):
            for child in value:
                walk(child, key)
        elif isinstance(value, str) and value.startswith("/"):
            raise ValueError("repair memory cannot store absolute paths")
    walk(constraints)
    for action in policy.actions:
        walk(action.arguments)


def _safe_copy(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))
