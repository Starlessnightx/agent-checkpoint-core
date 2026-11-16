from __future__ import annotations

from hashlib import sha256
import json
import math
import operator
from pathlib import Path
from typing import Any
from uuid import uuid4

from harness.executor.base import Executor


OPERATORS = {">=": operator.ge, "<=": operator.le, ">": operator.gt,
             "<": operator.lt, "==": operator.eq}


class EvidenceReviews:
    """Inspection receipts bind a task contract to real file bytes.

    Deterministic checks verify declared metrics/provenance. Scientific relevance
    remains an explicit model assessment, not a claim about hidden reasoning.
    """

    def __init__(self, executor: Executor, root: Path):
        self.executor, self.root = executor, root
        self._receipts: dict[str, dict[str, Any]] = {}
        self._collected: set[str] = set()
        self._presented: set[str] = set()

    def checkpoint_state(self) -> dict[str, Any]:
        from copy import deepcopy
        return {"schema_version": 1, "root": str(self.root), "receipts": deepcopy(self._receipts),
                "collected": sorted(self._collected), "presented": sorted(self._presented)}

    def collected(self, agent_id: str) -> None:
        self._collected.add(agent_id)

    def restore_checkpoint_state(self, value: dict[str, Any]) -> None:
        """Load verified archival data; never assert current evidence validity."""
        from .checkpoint_state import restore_evidence
        restore_evidence(self, value)

    def presented(self, model_view: str) -> None:
        for identifier, receipt in self._receipts.items():
            if identifier in model_view and all(digest in model_view for digest in receipt["file_hashes"].values()):
                self._presented.add(identifier)

    def path(self, ref: str) -> Path:
        path = (self.executor.workspace / ref).resolve()
        if not path.is_relative_to(self.executor.workspace.resolve()):
            raise ValueError("evidence reference escapes workspace")
        if not self.executor.is_file(path):
            raise ValueError(f"evidence file does not exist: {ref}")
        return path

    @staticmethod
    def contract(task: dict[str, Any]) -> str:
        fields = ("id", "owner", "delegated_agent_id", "acceptance", "evidence_refs", "source_refs", "metric_checks", "experiment_id")
        return sha256(json.dumps({key: task.get(key) for key in fields}, sort_keys=True).encode()).hexdigest()

    def inspect(self, task: dict[str, Any]) -> dict[str, Any]:
        refs = task.get("evidence_refs", [])
        if not refs:
            raise ValueError("set evidence_refs in the plan before inspecting")
        owner = task.get("delegated_agent_id") or task.get("owner", "parent")
        if owner not in {"", "parent"} and owner not in self._collected:
            raise ValueError("parent must collect the delegated result before inspecting evidence")
        content: dict[str, bytes] = {}
        for ref in dict.fromkeys(refs + task.get("source_refs", [])):
            content[ref] = self.executor.read_file(self.path(ref)).content
        hashes = {ref: sha256(raw).hexdigest() for ref, raw in content.items()}
        source_hashes = {ref: hashes[ref] for ref in task.get("source_refs", [])}
        checks = []
        for criterion in task.get("metric_checks", []):
            ref = criterion["ref"]
            if ref not in refs:
                raise ValueError("metric result must be one of the evidence_refs")
            if not source_hashes or not task.get("experiment_id"):
                raise ValueError("metric evidence requires source_refs and experiment_id")
            result = json.loads(content[ref])
            if result.get("run_id") != task["experiment_id"]:
                raise ValueError("metric evidence belongs to a different experiment")
            provenance = (result.get("provenance") or {}).get("source_hashes", {})
            def recorded_hash(ref: str):
                relative = self.path(ref).relative_to(self.executor.workspace.resolve()).as_posix()
                prefix = f".harness/subagents/{owner}/workspace/"
                local = relative[len(prefix):] if relative.startswith(prefix) else relative
                return provenance.get(ref, provenance.get(local))
            if any(recorded_hash(ref) != digest for ref, digest in source_hashes.items()):
                raise ValueError("metric provenance does not match current source/configuration hashes")
            actual: Any = result
            for part in criterion["key"].split("."):
                actual = actual[part]
            threshold = criterion["threshold"]
            if (isinstance(actual, bool) or not isinstance(actual, (int, float))
                    or not math.isfinite(actual) or not math.isfinite(threshold)):
                raise ValueError("metric and threshold must be finite numbers")
            checks.append({**criterion, "actual": actual,
                           "passed": bool(OPERATORS[criterion["operator"]](actual, threshold))})
        identifier = uuid4().hex
        receipt = {"receipt_id": identifier, "task_id": task["id"],
                   "contract_hash": self.contract(task), "file_hashes": hashes,
                   "metric_results": checks,
                   "previews": {ref: content[ref].decode(errors="replace")[:8000] for ref in refs},
                   "preview_note": "Previews may be truncated; read full files with read_file/read_observation when needed. File hashes cover full bytes."}
        self.executor.write_file(self.root / f"{identifier}.json",
                                 json.dumps(receipt, ensure_ascii=False, indent=2).encode())
        self._receipts[identifier] = receipt
        return receipt

    def validate(self, task: dict[str, Any]) -> None:
        review = task.get("evidence_review") or {}
        receipt = self._receipts.get(review.get("receipt_id", ""))
        if receipt is None:
            raise ValueError("inspect_plan_evidence must return a receipt before completing a task")
        if receipt["receipt_id"] not in self._presented:
            raise ValueError("evidence receipt must reach a subsequent model turn before assessment")
        if receipt["contract_hash"] != self.contract(task):
            raise ValueError("plan evidence/owner/acceptance/configuration changed; inspect again")
        if not str(review.get("assessment", "")).strip():
            raise ValueError("parent must explain what the evidence establishes and its limitations")
        outcome = review.get("outcome")
        if outcome not in {"supported", "refuted", "inconclusive"}:
            raise ValueError("record a supported/refuted/inconclusive evidence outcome")
        if outcome == "supported" and any(not result["passed"] for result in receipt["metric_results"]):
            raise ValueError("metric criteria failed; record refuted/inconclusive rather than supported")
        for ref, digest in receipt["file_hashes"].items():
            if sha256(self.executor.read_file(self.path(ref)).content).hexdigest() != digest:
                raise ValueError("evidence or source changed since inspection; inspect again")

    def invalidate(self) -> None:
        # A restored session must inspect again, not reuse old parent acceptance.
        self._receipts.clear()
        self._collected.clear()
        self._presented.clear()
