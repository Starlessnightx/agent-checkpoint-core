from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ContextCheckpoint:
    task_contract: str
    completed_actions: tuple[str, ...]
    files_changed: tuple[str, ...]
    failures: tuple[str, ...]
    verification: tuple[str, ...]
    active_skills: tuple[str, ...]
    subagents: tuple[str, ...]
    active_processes: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    latest_failure: str
    verification_after_last_write: bool
    next_action: str

    def dictionary(self) -> dict[str, Any]:
        return asdict(self)

    def prompt(self, archive_ref: str) -> str:
        lines = [
            "Deterministic task checkpoint. Treat these facts as authoritative:",
            f"- Task contract: {self.task_contract}",
            f"- Completed actions: {_joined(self.completed_actions)}",
            f"- Files changed: {_joined(self.files_changed)}",
            f"- Failures: {_joined(self.failures)}",
            f"- Verification: {_joined(self.verification)}",
            f"- Active skills: {_joined(self.active_skills)}",
            f"- Subagents: {_joined(self.subagents)}",
            f"- Active processes: {_joined(self.active_processes)}",
            f"- Archived evidence: {archive_ref}",
            f"- Other evidence refs: {_joined(self.evidence_refs)}",
            f"- Latest Tool failure: {self.latest_failure or 'none recorded'}",
            f"- Verification after last write: {self.verification_after_last_write}",
            f"- Next action: {self.next_action}",
        ]
        return "\n".join(lines)


class TaskLedger:
    """Derive non-LLM facts from structured runtime events."""

    def __init__(self, task: str):
        self.task = task.strip()

    def checkpoint(self, events: list[dict[str, Any]]) -> ContextCheckpoint:
        completed: list[str] = []
        changed: set[str] = set()
        failures: list[str] = []
        verification: list[str] = []
        skills: list[str] = []
        subagents: list[str] = []
        processes: set[str] = set()
        evidence: set[str] = set()
        indexed_tools: list[tuple[int, dict[str, Any]]] = []

        for index, event in enumerate(events):
            if event.get("type") == "runtime_handles_invalidated":
                processes.clear()
                subagents.clear()
            archive = event.get("archive")
            if archive:
                evidence.add(str(archive))
            evidence.update(str(path) for path in event.get("artifacts") or [])
            evidence.update(str(path) for path in event.get("tool_output_archives") or [])
            if event.get("type") != "tool":
                continue
            indexed_tools.append((index, event))
            name = str(event.get("name", "tool"))
            if event.get("ok"):
                completed.append(name)
            else:
                kind = event.get("failure_kind") or "unknown"
                output = str(event.get("output", "")).splitlines()
                detail = output[0][:200] if output else ""
                failures.append(f"{name} [{kind}]: {detail}")
            delta = event.get("state_delta") or {}
            for field in ("created", "modified", "deleted"):
                changed.update(str(path) for path in delta.get(field, []))
            processes.update(str(handle) for handle in delta.get("processes_started", []))
            processes.difference_update(str(handle) for handle in delta.get("processes_stopped", []))
            proof = event.get("verification")
            if proof:
                target = proof.get("target", "check")
                code = proof.get("exit_code", event.get("exit_code"))
                verification.append(f"{target}: exit_code={code}")
            if name == "load_skill" and event.get("ok"):
                skill_name = (event.get("arguments") or {}).get("name")
                if skill_name and skill_name not in skills:
                    skills.append(str(skill_name))
            if name in {"spawn_subagent", "collect_subagent_result", "cancel_subagent"}:
                output = str(event.get("output", "")).splitlines()
                subagents.append(f"{name}: {(output[0] if output else '')[:160]}")

        latest_tool = indexed_tools[-1][1] if indexed_tools else None
        latest_failed = bool(latest_tool and latest_tool.get("ok") is False)
        latest_failure = ""
        if latest_failed and latest_tool:
            kind = latest_tool.get("failure_kind") or "unknown"
            output = str(latest_tool.get("output", "")).splitlines()
            latest_failure = (
                f"{latest_tool.get('name', 'tool')} [{kind}]: "
                f"{(output[0] if output else '')[:200]}"
            )
        write_indexes = [
            index for index, event in indexed_tools
            if event.get("ok") and (
                event.get("name") in {"write_file", "replace_text", "apply_patch"}
                or any((event.get("state_delta") or {}).get(field) for field in ("created", "modified", "deleted"))
            )
        ]
        successful_verification_indexes = [
            index for index, event in indexed_tools
            if event.get("ok") and event.get("verification")
        ]
        verification_after_last_write = bool(
            write_indexes
            and successful_verification_indexes
            and successful_verification_indexes[-1] > write_indexes[-1]
        )
        if latest_failed:
            next_action = "Diagnose the latest failure using its archived evidence."
        elif write_indexes and not verification_after_last_write:
            next_action = "Run a relevant verification on the current workspace."
        elif processes:
            next_action = "Confirm readiness or stop the remaining managed processes."
        else:
            next_action = "Continue the current implementation plan from recent context."
        return ContextCheckpoint(
            task_contract=self.task,
            completed_actions=tuple(_unique(completed)[-30:]),
            files_changed=tuple(sorted(changed)),
            failures=tuple(failures[-12:]),
            verification=tuple(verification[-12:]),
            active_skills=tuple(skills),
            subagents=tuple(subagents[-12:]),
            active_processes=tuple(sorted(processes)),
            evidence_refs=tuple(sorted(evidence)),
            latest_failure=latest_failure,
            verification_after_last_write=verification_after_last_write,
            next_action=next_action,
        )


def _unique(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _joined(values: tuple[str, ...]) -> str:
    return ", ".join(values) if values else "none recorded"
