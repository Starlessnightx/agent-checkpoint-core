"""Plain, immutable exports from public Harness component state APIs."""
from __future__ import annotations

from dataclasses import fields, is_dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from checkpoint_plugin.contracts import Boundary, canonical, content_id
from checkpoint_plugin.contracts.agent import FrozenAgentData
from checkpoint_plugin.errors import CapabilityError
from harness.core.boundaries import RuntimeBoundary
from harness.core.session import Session
from harness.core.submission import TurnControl
from harness.core.turn_context import TurnContext
from harness.spec import HarnessSpec
from harness.protocol import Steer
from harness.context.summarizer import ModelSummarizer


def plain(value: Any):
    """No pickle, repr fallback, live objects, credentials or executable state."""
    if value is None or type(value) in (str, bool, int, float):
        return value
    if isinstance(value, Enum):
        return plain(value.value)
    if isinstance(value, Path):
        return str(value)
    if is_dataclass(value) and not isinstance(value, type):
        return {field.name: plain(getattr(value, field.name)) for field in fields(value)}
    if type(value) is dict and all(type(key) is str for key in value):
        return {key: plain(item) for key, item in value.items()}
    if type(value) in (tuple, list):
        return [plain(item) for item in value]
    if type(value) in (set, frozenset):
        return sorted((plain(item) for item in value), key=canonical)
    raise CapabilityError("nonportable state: " + type(value).__module__ + ":" + type(value).__qualname__)


def _complete_groups(messages):
    pending = set()
    for message in messages:
        if message.get("role") == "tool":
            identifier = message.get("tool_call_id")
            if identifier not in pending:
                raise CapabilityError("orphan or duplicate tool result in conversation")
            pending.remove(identifier)
        else:
            if pending:
                raise CapabilityError("checkpoint intersects an incomplete tool group")
            seen = set()
            for call in message.get("tool_calls") or []:
                identifier = call.get("id")
                if type(identifier) is not str or not identifier or identifier in seen:
                    raise CapabilityError("tool call identities are not unique")
                seen.add(identifier)
                pending.add(identifier)
    if pending:
        raise CapabilityError("checkpoint intersects an incomplete tool group")


class HarnessStateAdapter:
    version = "1"

    def __init__(self, spec: HarnessSpec, *, model_exporter: Callable[[Any, str], dict] | None = None):
        self.spec = spec
        self.model_exporter = model_exporter

    def capabilities(self) -> tuple[str, ...]:
        return ("harness-turn-agent-state-v1", "joined-runtime-boundaries-v1")

    def freeze(self, session: Session, boundary: RuntimeBoundary, context: TurnContext,
               control: TurnControl) -> FrozenAgentData:
        control.check_active()
        if session.closed or session.active_turn_id != boundary.turn_id or control.turn_id != boundary.turn_id:
            raise CapabilityError("checkpoint does not own the active turn")
        if control.pending_request_count:
            raise CapabilityError("pending frontend request cannot be checkpointed")
        incoming = session.submission_loop.pending_operations if session.submission_loop is not None else ()
        if any(type(item) is not Steer or item.turn_id != control.turn_id for item in incoming):
            raise CapabilityError("undispatched frontend control operation prevents a checkpoint")
        if context.worker_cancelled.is_set():
            raise CapabilityError("cancelled worker context cannot be checkpointed")
        if self.spec.model.responses_previous_response_id:
            raise CapabilityError("provider-side continuation requires a checkpoint capability provider")
        if self.model_exporter is not None:
            provider = self.model_exporter(session.model, session.session_id)
        else:
            export_model = getattr(session.model, "checkpoint_state", None)
            if not callable(export_model):
                raise CapabilityError("custom model requires an explicit checkpoint state exporter")
            provider = export_model(session.session_id)
        if (boundary.conversation_cursor != len(session.messages)
                or session.recorder.offset and session.recorder.offset != len(session.recorder.history)-len(session.recorder.events)
                or not 0 <= boundary.event_cursor <= len(session.events)):
            raise CapabilityError("checkpoint cursor is stale or has an unsupported inherited event prefix")
        _complete_groups(session.messages)
        summarizer = session.context_manager.summarizer
        if summarizer is not None and (type(summarizer) is not ModelSummarizer or summarizer.model is not session.model):
            raise CapabilityError("custom summarizer requires an explicit checkpoint capability provider")
        context_state = session.context_manager.checkpoint_state()
        background = session.tools.context.background_tasks
        # These public exports reject live work, rather than serializing Futures
        # or accepting the model's claim that an external process has stopped.
        child_state = session.subagents.checkpoint_state()
        processes = session.tools.context.executor.checkpoint_processes()
        if any(process.running for process in processes):
            raise CapabilityError("managed executor process is still running")
        background_state = background.checkpoint_state(processes=processes) if background is not None else None
        mcp = session.mcp_manager.checkpoint_state() if session.mcp_manager is not None else None
        lsp = session.lsp_manager.checkpoint_state() if session.lsp_manager is not None else None
        plan = session.tools.context.plan_store
        archives = [("api_audit", session.recorder.path.parent / "api")]
        archives += [("api_audit_prefix_" + str(i), path) for i, path in enumerate(session.api_audit_prefixes)]
        observations = session.context_manager.observations
        archives += [("observations_" + str(i), path) for i, path in
                     enumerate((observations.root, *observations.fallbacks))]
        if plan is not None:
            archives.append(("plans", plan.root))
        for identifier, value in child_state["results"].items():
            result = value.get("result")
            if result and result.get("trajectory_path"):
                archives.append(("child_" + content_id(identifier.encode())[:16], Path(result["trajectory_path"]).parent))
        for item in processes:
            archives.append(("process_" + content_id(item.handle.encode())[:16], item.log_path))
        data = {
            "conversation": session.messages,
            "model_view": {"current": session.model_context, "last": session.context_manager.last_model_view},
            "context": context_state,
            "task": {"contract": session.task.full_dictionary(), "runtime_state": session.state,
                     "environment": session.environment,
                     "policies": {"orchestrator": session.orchestrator.checkpoint_state(),
                                  "environment_repair": session.environment_repair.checkpoint_state()},
                     "artifacts": sorted(session.artifacts.paths), "session_id": session.session_id,
                     "parent_session_id": session.parent_session_id, "agent_role": session.agent_role,
                     "boundary": boundary, "sampling_index": session.sampling_index,
                     "turn_context": {"steps": context.steps, "unsupported_output_notices": context.unsupported_output_notices},
                     "regular_task_initial_consumed": True},
            "plan": plan.checkpoint_state() if plan is not None else None,
            "tools": {"registry": session.tools.checkpoint_state(), "hooks": session.hooks.checkpoint_state() if session.hooks else None,
                      "observed_environment": session.tools.context.environment_state,
                      "background": background_state, "executor_processes": processes, "mcp": mcp, "lsp": lsp},
            "budget": {"configured": session.budget, "token_budget": session.token_budget,
                       "tool_call_budget": session.tool_call_budget, "orchestration": session.orchestrator.policy,
                       "recovery_policy": session.recovery.policy},
            "usage": {"session": session.usage.dictionary(),
                      "task": session.task_usage.snapshot() if session.task_usage else None},
            "pending_inputs": {"turn": [item.dictionary() for item in control.inputs.snapshot()],
                               "incoming": [item.dictionary() for item in incoming],
                               "deferred": [item.dictionary() for item in session.deferred_inputs]},
            "completed_children": child_state,
            "recovery": session.recovery.checkpoint_state(),
            "api_audit": {"archive_labels": [label for label, _ in archives]},
            "raw_events": {"offset": 0, "event_cursor": boundary.event_cursor,
                           "events": session.events[:boundary.event_cursor]},
            "configuration": {"context": self.spec.context, "model": self.spec.model, "provider": provider,
                              "workspace": str(self.spec.workspace), "prompt_path": str(self.spec.prompt_path)},
        }
        # Canonical bytes own their data. Subsequent steering/usage/audit changes
        # cannot mutate a frame being persisted by a worker thread.
        position = Boundary(boundary.kind, boundary.event_cursor, session.sampling_index)
        return FrozenAgentData("harness-turn", self.version, position,
                               tuple((name, canonical(plain(value))) for name, value in data.items()),
                               tuple((name, Path(path) if Path(path).is_absolute() else self.spec.workspace / path)
                                     for name, path in archives))
