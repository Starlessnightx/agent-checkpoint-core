from __future__ import annotations

import difflib
import asyncio
import shutil
import threading
import uuid
from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import replace
from pathlib import Path
from typing import Any

from safety.audit import append_event
from safety.boundaries import ensure_within
from harness.types import ChatModel

from .base import SubagentResult, SubagentSpec, WorkspaceMode
from .registry import default_subagents


class SubagentCoordinator:
    """Run bounded agents with isolated context, capability permissions, and audit trails."""

    def __init__(
        self,
        model: ChatModel,
        workspace: Path,
        *,
        max_parallel: int = 3,
        specs: tuple[SubagentSpec, ...] | None = None,
        runtime: Any = None,
    ):
        if max_parallel < 1:
            raise ValueError("max_parallel must be positive")
        self.model = model
        self.workspace = workspace.resolve()
        self.specs = {item.name: item for item in (specs or default_subagents())}
        self.max_parallel = max_parallel
        self.runtime = runtime
        self.parent_session: Any = None
        self.executor = ThreadPoolExecutor(
            max_workers=max_parallel, thread_name_prefix="harness-subagent"
        )
        self._futures: dict[str, Future[SubagentResult]] = {}
        self._roles: dict[str, str] = {}
        self._cancelled: set[str] = set()
        self._steers: dict[str, list[str]] = {}
        self._lock = threading.Lock()
        self._notifications: list[dict[str, Any]] = []
        self._notified: set[str] = set()
        self._task_ids: dict[str, str] = {}
        self._historical_ids: set[str] = set()
        self.plan_store: Any = None

    def agent_ids(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(self._futures)

    def checkpoint_state(self) -> dict[str, Any]:
        """Only completed child data is portable; no Future or worker is saved."""
        for identifier in self.agent_ids():
            future = self._future(identifier)
            if not future.done():
                raise RuntimeError("cannot checkpoint an active child agent")
            self._notify_completion(identifier, future)
        with self._lock:
            if any(not future.done() for future in self._futures.values()):
                raise RuntimeError("child scheduler changed during checkpoint export")
            results = {}
            for identifier, future in self._futures.items():
                try:
                    results[identifier] = {"result": future.result().dictionary()}
                except Exception as exc:
                    from .state import ArchivedChildFailure
                    results[identifier] = {"error_type": exc.source_type if isinstance(exc, ArchivedChildFailure)
                                           else type(exc).__name__, "error": str(exc)}
            from copy import deepcopy
            from dataclasses import asdict
            return deepcopy({"schema_version": 1, "results": results, "roles": self._roles,
                             "cancelled": sorted(self._cancelled), "steers": self._steers,
                             "notifications": self._notifications, "notified": sorted(self._notified),
                             "task_ids": self._task_ids, "max_parallel": self.max_parallel,
                             "specs": {name: asdict(spec) for name, spec in self.specs.items()}})

    def restore_completed_checkpoint_state(self, value: dict[str, Any], *, archive_paths: dict[str, Path]) -> None:
        """Restore completed results and delivery state, never child workers."""
        from .state import restore_children
        restore_children(self, value, archive_paths=archive_paths)

    def associate_task(self, agent_id: str, task_id: str) -> None:
        with self._lock:
            self._task_ids[agent_id] = task_id

    def take_notifications(self) -> list[dict[str, Any]]:
        # Reconcile completed futures too: future.result() may return before its
        # done callback has run. Callback and reconciliation share deduplication.
        for identifier in self.agent_ids():
            future = self._future(identifier)
            if future.done():
                self._notify_completion(identifier, future)
        with self._lock:
            notices, self._notifications = self._notifications, []
            return [{**notice, "plan_task_id": self._task_ids.get(notice["agent_id"])}
                    for notice in notices]

    def _notify_completion(self, agent_id: str, done) -> None:
        try:
            result = done.result()
            notice = {"kind": "subagent", "agent_id": agent_id,
                      "status": result.status, "summary": result.summary[:2000],
                      "evidence_refs": list(result.evidence_refs),
                      "trajectory_path": result.trajectory_path, "usage": result.usage}
        except Exception as exc:
            notice = {"kind": "subagent", "agent_id": agent_id,
                      "status": "failed", "summary": str(exc)}
        with self._lock:
            if agent_id not in self._notified:
                self._notified.add(agent_id)
                self._notifications.append(notice)

    def bind_parent(self, session: Any) -> None:
        self.parent_session = session

    def roles(self) -> list[dict[str, Any]]:
        return [{
            "name": spec.name,
            "description": spec.description,
            "workspace_modes": list(spec.workspace_modes),
            "skills": list(spec.skills),
            "tools": list(spec.tools),
            "max_steps": spec.max_steps,
            "max_tool_calls": spec.max_tool_calls,
            "max_context_tokens": (
                spec.max_context_tokens
                if spec.max_context_tokens is not None
                else getattr(
                    getattr(self.model, "limits", None), "input_capacity", None
                )
            ),
        } for spec in self.specs.values()]

    def spawn(
        self,
        role: str,
        task: str,
        context_refs: list[str],
        workspace_mode: str | None = None,
        *,
        agent_spec: Any = None,
    ) -> str:
        if role not in self.specs:
            raise ValueError(f"unknown subagent role: {role}")
        if not task.strip():
            raise ValueError("subagent task must be non-empty")
        spec = self.specs[role]
        mode = workspace_mode or spec.default_workspace_mode
        if mode not in spec.workspace_modes:
            raise ValueError(f"role {role} does not permit workspace mode {mode}")
        context = self._read_context_refs(context_refs)
        with self._lock:
            active = sum(not future.done() for future in self._futures.values())
            if active >= self.max_parallel:
                raise RuntimeError(
                    f"subagent concurrency limit reached: {active}/{self.max_parallel}"
                )
        for _ in range(16):
            agent_id = f"{role}-{uuid.uuid4().hex[:10]}"
            with self._lock:
                if agent_id not in self._futures:
                    break
        else:
            raise RuntimeError('cannot allocate a unique child identity')
        resolved_agent = self._resolve_agent_spec(spec, mode, agent_spec)
        future = self.executor.submit(
            self._run, agent_id, resolved_agent, task.strip(), context, mode
        )
        with self._lock:
            self._futures[agent_id] = future
            self._roles[agent_id] = role
        def completed(done):
            self._notify_completion(agent_id, done)
        future.add_done_callback(completed)
        return agent_id

    def _resolve_agent_spec(self, role: SubagentSpec, mode: WorkspaceMode, supplied: Any) -> Any:
        from .manager import AgentSpec

        if supplied is None:
            return AgentSpec(
                name=role.name,
                model="default",
                system_prompt=Path(role.prompt_path).read_text(encoding="utf-8"),
                capabilities=role.tools,
                permission=("read_only" if mode == "read_only" else "workspace_write"),
                max_turns=role.max_steps,
                token_budget=None,
                workspace_policy=mode,
                skills=role.skills,
                max_tool_calls=role.max_tool_calls,
                context_policy=role.context,
                max_context_tokens=role.max_context_tokens,
            )
        if supplied.name != role.name:
            raise ValueError("AgentSpec name must match the selected registered role")
        allowed_tools = set(role.tools)
        allowed_skills = set(role.skills)
        return replace(
            supplied,
            capabilities=tuple(
                name for name in supplied.capabilities if name in allowed_tools
            ),
            skills=tuple(name for name in supplied.skills if name in allowed_skills),
            max_turns=_optional_min(supplied.max_turns, role.max_steps),
            token_budget=supplied.token_budget,
            max_tool_calls=_optional_min(supplied.max_tool_calls, role.max_tool_calls),
            workspace_policy=mode,
            context_policy=supplied.context_policy or role.context,
            max_context_tokens=(
                supplied.max_context_tokens
                if supplied.max_context_tokens is not None
                else role.max_context_tokens
            ),
        )

    def status(self, agent_id: str) -> dict[str, Any]:
        future = self._future(agent_id)
        if agent_id in self._cancelled and not future.done():
            state = "cancel_requested"
        elif future.running():
            state = "running"
        elif not future.done():
            state = "queued"
        elif future.cancelled():
            state = "cancelled"
        else:
            state = future.result().status
        return {
            "agent_id": agent_id,
            "role": self._roles.get(agent_id, "unknown"),
            "state": state,
            "done": future.done(),
        }

    def collect(self, agent_id: str, timeout_seconds: float = 0) -> SubagentResult | None:
        future = self._future(agent_id)
        if timeout_seconds == 0 and not future.done():
            return None
        try:
            return future.result(timeout=None if timeout_seconds < 0 else timeout_seconds)
        except FutureTimeout:
            return None

    def cancel(self, agent_id: str) -> bool:
        future = self._future(agent_id)
        if agent_id in self._historical_ids:
            emit = getattr(self.parent_session, 'emit', None)
            if callable(emit):
                emit('checkpoint_historical_child_control_ignored', agent_id=agent_id, action='cancel',
                     reason='completed child has no restored execution authority')
            return False
        with self._lock:
            self._cancelled.add(agent_id)
        append_event(self.workspace / ".harness" / "subagents" / agent_id / "trajectory.jsonl", {
            "type": "subagent_control", "agent_id": agent_id, "action": "cancel",
        })
        # Use cooperative cancellation so collect() always receives a structured
        # SubagentResult instead of concurrent.futures.CancelledError.
        return not future.done()

    def steer(self, agent_id: str, instruction: str) -> bool:
        if not instruction.strip():
            raise ValueError("steering instruction must be non-empty")
        future = self._future(agent_id)
        with self._lock:
            if future.done():
                return False
            self._steers.setdefault(agent_id, []).append(instruction.strip())
        append_event(self.workspace / ".harness" / "subagents" / agent_id / "trajectory.jsonl", {
            "type": "subagent_control", "agent_id": agent_id, "action": "steer",
            "instruction_preview": instruction.strip()[:400],
        })
        return True

    def close(self, wait: bool = True) -> None:
        self.executor.shutdown(wait=wait, cancel_futures=True)

    def _future(self, agent_id: str) -> Future[SubagentResult]:
        with self._lock:
            future = self._futures.get(agent_id)
        if future is None:
            raise ValueError(f"unknown subagent: {agent_id}")
        return future

    def _read_context_refs(self, refs: list[str]) -> str:
        sections: list[str] = []
        total = 0
        for relative in refs:
            path = ensure_within(self.workspace / relative, self.workspace)
            if not path.is_file():
                raise ValueError(f"context reference is not a file: {relative}")
            text = path.read_text(encoding="utf-8", errors="replace")
            if len(text) > 12_000:
                text = text[:12_000] + "\n[context reference truncated at 12000 characters]"
            total += len(text)
            if total > 40_000:
                raise ValueError("subagent context references exceed 40000 characters")
            sections.append(f"## {relative}\n{text}")
        return "\n\n".join(sections)

    def _run(
        self,
        agent_id: str,
        agent_spec: Any,
        task: str,
        context: str,
        mode: WorkspaceMode,
    ) -> SubagentResult:
        shadow: Path | None = None
        run_directory = self.workspace / ".harness" / "subagents" / agent_id
        trajectory = run_directory / "trajectory.jsonl"
        run_directory.mkdir(parents=True, exist_ok=True)
        active_workspace = self.workspace
        child_session = None
        final = "Subagent runtime failed."
        status: str = "failed"
        stop_reason = "runtime_error"
        error: str | None = None

        try:
            if mode == "patch_only":
                shadow = self._shadow_workspace(agent_id)
                active_workspace = shadow
            runtime = self._runtime_instance()
            child_session = runtime.create_child_session(
                self.parent_session,
                agent_spec,
                task,
                context,
                agent_id=agent_id,
                workspace=active_workspace,
            )

            def control() -> dict[str, Any]:
                with self._lock:
                    steering = self._steers.pop(agent_id, [])
                    cancelled = agent_id in self._cancelled
                return {"cancel": cancelled, "steering": steering}

            from harness.tasks.delegated import run_delegated

            agent_result = asyncio.run(run_delegated(runtime, child_session, control))
            final = agent_result.text
            end = next((event for event in reversed(agent_result.events) if event.get("type") == "run_end"), {})
            termination = end.get("termination") or child_session.state.termination or "runtime_error"
            from harness.core.budgets import _session_budget_reason

            if termination == "completed":
                status, stop_reason = "completed", "final_response"
            elif termination in {"cancellation_requested", "interrupted"}:
                status, stop_reason = "cancelled", "cancellation_requested"
            elif termination == "budget_limited" and _session_budget_reason(child_session):
                status, stop_reason = "failed", "budget_exhausted"
            elif termination == "budget_limited" and child_session.tool_call_budget is not None and sum(
                event.get("type") == "tool" for event in agent_result.events
            ) >= child_session.tool_call_budget:
                status, stop_reason = "failed", "max_tool_calls"
            elif termination in {"maximum_turns", "budget_limited"}:
                status, stop_reason = "failed", "max_steps"
            else:
                status, stop_reason = "failed", termination
        except Exception as exc:
            status, stop_reason = "failed", "runtime_error"
            error = f"{type(exc).__name__}: {exc}"
        finally:
            if child_session is not None:
                child_session.close()

        events = list(child_session.events) if child_session is not None else []
        steps = child_session.state.turn if child_session is not None else 0
        tool_calls = sum(event.get("type") == "tool" for event in events)
        compactions = sum(
            event.get("type") in {"compaction", "context_compaction"}
            for event in events
        )
        patch = self._workspace_diff(self.workspace, shadow) if shadow else ""
        evidence = _evidence_refs(events, active_workspace, self.workspace)
        if patch:
            patch_path = run_directory / "change.patch"
            patch_path.write_text(patch, encoding="utf-8")
            evidence.add(str(patch_path.relative_to(self.workspace)))
        trajectory_path = str(trajectory.relative_to(self.workspace))
        evidence.add(trajectory_path)
        verification = tuple(
            event.get("verification_evidence") or event.get("verification")
            for event in events
            if event.get("verification_evidence") or event.get("verification")
        )
        artifacts = set(child_session.artifacts.paths) if child_session else set()
        artifacts.update(
            str(path) for event in events for path in (event.get("artifacts") or ())
        )
        result_event = {
            "type": "subagent_result",
            "agent_id": agent_id,
            "role": agent_spec.name,
            "status": status,
            "stop_reason": stop_reason,
            "steps": steps,
            "tool_calls": tool_calls,
            "compactions": compactions,
            "trajectory_path": trajectory_path,
        }
        append_event(trajectory, result_event)
        return SubagentResult(
            agent_id=agent_id,
            role=agent_spec.name,
            status=status,  # type: ignore[arg-type]
            summary=final,
            workspace_mode=mode,
            steps=steps,
            tool_calls=tool_calls,
            patch=_bounded_patch(patch),
            error=error,
            stop_reason=stop_reason,
            trajectory_path=trajectory_path,
            workspace_path=str(active_workspace.relative_to(self.workspace)) or ".",
            evidence_refs=tuple(sorted(evidence)),
            verification=verification,
            artifacts=tuple(sorted(artifacts)),
            usage=(child_session.usage.dictionary() if child_session else {}),
            compactions=compactions,
            events=tuple(_bounded_event(event) for event in events[-100:]),
        )

    def _runtime_instance(self) -> Any:
        if self.runtime is None:
            from harness.core.runtime import AgentRuntime
            from harness.spec import build_spec

            root = Path(__file__).resolve().parents[2]
            self.runtime = AgentRuntime(build_spec(root, self.workspace), self.model)
        return self.runtime

    def _shadow_workspace(self, agent_id: str) -> Path:
        destination = self.workspace / ".harness" / "subagents" / agent_id / "workspace"
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            self.workspace,
            destination,
            ignore=shutil.ignore_patterns(
                ".git", ".harness", ".venv", "node_modules", "__pycache__"
            ),
        )
        return destination

    @staticmethod
    def _workspace_diff(original: Path, shadow: Path | None) -> str:
        if shadow is None:
            return ""
        original_files = _text_files(original)
        shadow_files = _text_files(shadow)
        chunks: list[str] = []
        for relative in sorted(original_files | shadow_files):
            before = _read_lines(original / relative) if relative in original_files else []
            after = _read_lines(shadow / relative) if relative in shadow_files else []
            if before == after:
                continue
            chunks.extend(difflib.unified_diff(
                before,
                after,
                fromfile=f"a/{relative}" if relative in original_files else "/dev/null",
                tofile=f"b/{relative}" if relative in shadow_files else "/dev/null",
                lineterm="",
            ))
        return "\n".join(chunks)


def _evidence_refs(events: list[dict[str, Any]], active: Path, parent: Path) -> set[str]:
    result: set[str] = set()
    prefix = str(active.relative_to(parent)) if active != parent else ""
    for event in events:
        for supplied in event.get("artifacts") or []:
            value = Path(str(supplied))
            result.add(str(Path(prefix) / value) if prefix and not value.is_absolute() else str(value))
        archive = event.get("archive")
        if archive:
            value = Path(str(archive))
            try:
                result.add(str(value.relative_to(parent)) if value.is_absolute() else str(value))
            except ValueError:
                result.add(str(value))
    return result


def _bounded_event(event: dict[str, Any]) -> dict[str, Any]:
    value = dict(event)
    if "output" in value:
        output = str(value["output"])
        value["output"] = output[:1000] + ("\n[event output truncated]" if len(output) > 1000 else "")
    return value


def _bounded_patch(patch: str) -> str:
    limit = 200_000
    if len(patch) <= limit:
        return patch
    return patch[:limit] + "\n[patch preview truncated; read change.patch from evidence_refs]"


def _text_files(root: Path) -> set[str]:
    result: set[str] = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if not path.is_file() or any(
            part in {".git", ".harness", ".venv", "node_modules", "__pycache__"}
            for part in relative.parts
        ):
            continue
        if path.stat().st_size > 1_000_000:
            continue
        try:
            path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        result.add(str(relative))
    return result


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content or "")


def _optional_min(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return min(left, right)
