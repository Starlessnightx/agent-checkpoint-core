"""Scoped tool-worker ↔ submission-channel bridge, not an authorization kernel."""
from __future__ import annotations

import asyncio
from concurrent.futures import CancelledError, TimeoutError
import copy
import json
from threading import Event
from typing import Any, TYPE_CHECKING
from uuid import uuid4

from harness.permissions import PermissionPolicy, PermissionResult
from harness.protocol import ApprovalResponse, EventKind, UserInputAnswer
from harness.tools.base import ToolContext, ToolRequest, ToolResult, ToolSpec
from harness.tools.user_input import user_input_spec
from .submission import TurnControl
from .pending_requests import Reply

if TYPE_CHECKING:
    from harness.tools.registry import ToolRegistry


class TurnInteractions:
    def __init__(self, control: TurnControl, cancelled: Event, *, interactive: bool,
                 preapprove_tool_hooks: bool = False):
        self.control, self.cancelled, self.interactive = control, cancelled, interactive
        self.loop = asyncio.get_running_loop()
        self.preapprove_tool_hooks = preapprove_tool_hooks

    def _request(self, kind: EventKind, text: str, details: dict[str, Any], *,
                 answer_ids: frozenset[str] | None = None) -> Reply | None:
        if not self.interactive or self.cancelled.is_set():
            return None
        # Called only in a tool worker. Never block the owning asyncio thread.
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None
        if running_loop is self.loop:
            raise RuntimeError("interaction bridge requires a tool worker")
        future = asyncio.run_coroutine_threadsafe(self.control.request(
            str(uuid4()), kind, text, details=copy.deepcopy(details), answer_ids=answer_ids,
        ), self.loop)
        try:
            while not self.cancelled.is_set():
                try:
                    return future.result(timeout=0.05)
                except TimeoutError:
                    continue
                except CancelledError:
                    return None
            return None
        finally:
            if not future.done():
                future.cancel()

    def approve(self, *, name: str, arguments: dict[str, Any], call_id: str,
                reason: str, source: str) -> bool:
        details = {"tool_name": name, "arguments": copy.deepcopy(arguments),
                   "tool_call_id": call_id, "reason": reason, "source": source}
        reply = self._request(EventKind.APPROVAL_REQUESTED, reason, details)
        approved = isinstance(reply, ApprovalResponse) and reply.approved and not self.cancelled.is_set()
        self.control._emit("tool_approval_decision", turn_id=self.control.turn_id,
                           **details, approved=approved,
                           interactive=self.interactive)
        return approved

    def approve_hook(self, name: str, arguments: dict[str, Any], call_id: str,
                     reason: str) -> bool:
        if self.preapprove_tool_hooks:
            approved = not self.cancelled.is_set()
            self.control._emit("tool_approval_decision", turn_id=self.control.turn_id,
                               tool_name=name, arguments=copy.deepcopy(arguments), tool_call_id=call_id,
                               reason=reason, source="configured_policy_hook", approved=approved,
                               interactive=self.interactive)
            return approved
        return self.approve(name=name, arguments=arguments, call_id=call_id,
                            reason=reason, source="hook")

    def approve_model_hook(self, model_name: str, model_request_id: str, reason: str) -> bool:
        """Model approval is turn-scoped and never inherits tool preauthorization."""
        details = {"request_kind": "model", "model_name": model_name,
                   "model_request_id": model_request_id, "reason": reason, "source": "model_hook"}
        reply = self._request(EventKind.APPROVAL_REQUESTED, reason, details)
        approved = isinstance(reply, ApprovalResponse) and reply.approved and not self.cancelled.is_set()
        self.control._emit("model_approval_decision", turn_id=self.control.turn_id,
                           **details, approved=approved, interactive=self.interactive)
        return approved

    def ask(self, questions: list[dict[str, str]]) -> ToolResult:
        if not self.interactive:
            return ToolResult(False, "User input is unavailable in headless mode.",
                              failure_kind="user_input_unavailable")
        reply = self._request(EventKind.USER_INPUT_REQUESTED, "Please answer the requested questions.",
                              {"questions": questions},
                              answer_ids=frozenset(q["id"] for q in questions))
        if not isinstance(reply, UserInputAnswer):
            return ToolResult(False, "User input request was cancelled.", failure_kind="cancelled")
        answers = dict(reply.answers)
        return ToolResult(True, "User answered the questions.",
                          stdout=json.dumps({"answers": answers}, ensure_ascii=False),
                          structured={"answers": answers})


class InteractionPermissionPolicy(PermissionPolicy):
    """Delegate all permission decisions; only supply an ASK response channel."""
    def __init__(self, base: PermissionPolicy, interaction: TurnInteractions):
        super().__init__(base.maximum)
        self.base, self.interaction = base, interaction

    def evaluate(self, tool: ToolSpec, request: ToolRequest, context: ToolContext) -> PermissionResult:
        result = self.base.evaluate(tool, request, context)
        self.interaction.control._emit("permission_decision", turn_id=self.interaction.control.turn_id,
                                       tool_name=request.name, tool_call_id=request.call_id,
                                       permission=result.dictionary())
        return result

    def approve(self, result: PermissionResult, tool: ToolSpec, request: ToolRequest) -> bool:
        if not self.interaction.interactive:
            # Preserve explicitly configured automation; never synthesize approval.
            approved = self.base.approve(result, tool, request)
            self.interaction.control._emit("tool_approval_decision", turn_id=self.interaction.control.turn_id,
                                           tool_name=request.name, tool_call_id=request.call_id,
                                           source="configured_policy", approved=approved,
                                           interactive=False)
            return approved
        return self.interaction.approve(name=request.name, arguments=request.arguments,
                                        call_id=request.call_id, reason=result.reason,
                                        source="permission_policy")


class ToolCatalogScope:
    """The fixed per-turn catalog, without permission or interaction authority."""
    def __init__(self, registry: ToolRegistry, *, allow_user_input: bool = True):
        self.registry, self.allow_user_input = registry, allow_user_input

    def __enter__(self):
        registry = self.registry
        self.installed = (self.allow_user_input
                          and (registry.allowed_tools is None or 'request_user_input' in registry.allowed_tools)
                          and 'request_user_input' not in registry.specs)
        if self.installed:
            registry.register(user_input_spec())
            registry.router.activate(('request_user_input',))
        return self

    def __exit__(self, *exc):
        if self.installed:
            self.registry.specs.pop('request_user_input', None)
            self.registry.active_tool_names = tuple(
                name for name in self.registry.active_tool_names if name != 'request_user_input')


class ToolInteractionScope:
    """Install per-turn dependencies and restore them after all workers exit."""
    def __init__(self, registry: ToolRegistry, interaction: TurnInteractions, *,
                 allow_user_input: bool = True):
        self.registry, self.interaction = registry, interaction
        self.allow_user_input = allow_user_input

    def __enter__(self) -> ToolInteractionScope:
        registry = self.registry
        self.policy, self.previous = registry.permission_policy, registry.context.interaction
        self.catalog = ToolCatalogScope(registry, allow_user_input=self.allow_user_input)
        self.catalog.__enter__()
        registry.permission_policy = InteractionPermissionPolicy(self.policy, self.interaction)
        registry.context.interaction = self.interaction
        return self

    def __exit__(self, *exc) -> None:
        self.registry.permission_policy = self.policy
        self.registry.context.interaction = self.previous
        self.catalog.__exit__(*exc)
