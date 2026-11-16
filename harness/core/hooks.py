"""Ordered, isolated middleware for cross-cutting runtime policy."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, is_dataclass
from enum import Enum
from typing import Any, Protocol


@dataclass
class HookContext:
    session: Any
    event: str
    payload: dict[str, Any] = field(default_factory=dict)
    data: dict[str, Any] = field(default_factory=dict)
    stopped: bool = False
    errors: list[str] = field(default_factory=list)
    approval_resume_index: int | None = None


class HookAction(str, Enum):
    CONTINUE = "continue"
    BLOCK = "block"
    MODIFY_REQUEST = "modify_request"
    MODIFY_RESULT = "modify_result"
    INJECT_CONTEXT = "inject_context"
    REQUEST_APPROVAL = "request_approval"
    TRIGGER_REPAIR = "trigger_repair"


@dataclass(frozen=True)
class HookDecision:
    action: HookAction | bool = HookAction.CONTINUE
    reason: str = ""
    value: Any = None

    def __post_init__(self) -> None:
        # Compatibility adapter for the original HookDecision(False, reason).
        if isinstance(self.action, bool):
            object.__setattr__(
                self, "action",
                HookAction.CONTINUE if self.action else HookAction.BLOCK,
            )
        elif isinstance(self.action, str):
            object.__setattr__(self, "action", HookAction(self.action))

    @property
    def continue_processing(self) -> bool:
        return self.action not in {
            HookAction.BLOCK, HookAction.REQUEST_APPROVAL,
        }


class Middleware(Protocol):
    name: str
    order: int

    def handle(self, context: HookContext) -> HookDecision | None: ...


class HookPipeline:
    """Run middleware in deterministic order with exception isolation."""

    def __init__(self, middleware: tuple[Middleware, ...] = ()):
        self.middleware = tuple(sorted(middleware, key=lambda item: (item.order, item.name)))

    def checkpoint_state(self) -> dict[str, Any]:
        result = []
        for item in self.middleware:
            export = getattr(item, "checkpoint_state", None)
            if not callable(export):
                raise ValueError("middleware has no checkpoint exporter: " + item.name)
            result.append({"implementation": type(item).__module__ + ":" + type(item).__qualname__,
                           "name": item.name, "order": item.order, "state": export()})
        return {"schema_version": 1, "middleware": result}

    def restore_checkpoint_state(self, value: dict[str, Any]) -> None:
        """Load data only into deployment-assembled middleware implementations."""
        from .portable_state import copy_plain, require_fields
        value = copy_plain(value)
        require_fields(value, {'schema_version', 'middleware'})
        if (type(value['schema_version']) is not int or value['schema_version'] != 1
                or type(value['middleware']) is not list or len(value['middleware']) != len(self.middleware)):
            raise ValueError('middleware assembly differs')
        methods = []
        for prior, current, item in zip(value['middleware'], self.checkpoint_state()['middleware'], self.middleware):
            require_fields(prior, {'implementation', 'name', 'order', 'state'})
            if {k:v for k,v in prior.items() if k!='state'} != {k:v for k,v in current.items() if k!='state'}:
                raise ValueError('middleware implementation differs')
            restore = getattr(item, 'restore_checkpoint_state', None)
            if not callable(restore):
                raise ValueError('middleware requires an explicit data importer')
            methods.append((restore, prior['state']))
        for restore, state in methods:
            restore(state)

    def dispatch(self, event: str, session: Any, **payload: Any) -> HookContext:
        context = HookContext(session=session, event=event, payload=dict(payload))
        session.emit("hook_started", hook=event,
                     middleware=[item.name for item in self.middleware])
        return self._dispatch(context, 0)

    def resume_after_approval(self, context: HookContext) -> HookContext:
        """Resume remaining checks; approving one check never skips later ones."""
        start = context.approval_resume_index
        if start is None or not context.stopped:
            raise ValueError("hook is not suspended for approval")
        context.approval_resume_index = None
        context.stopped = False
        context.data.pop("approval", None)
        context.data.pop("stop_reason", None)
        context.session.emit("hook_resumed", hook=context.event, next_middleware=start)
        return self._dispatch(context, start)

    def _dispatch(self, context: HookContext, start: int) -> HookContext:
        session, event = context.session, context.event
        for index, item in enumerate(self.middleware[start:], start):
            try:
                decision = item.handle(context)
            except Exception as exc:  # middleware must never break the runtime
                message = f"{item.name}: {type(exc).__name__}: {exc}"
                context.errors.append(message)
                session.emit("hook_error", hook=event, middleware=item.name,
                             exception_type=type(exc).__name__, message=str(exc)[:4000])
                continue
            if decision is not None:
                self._apply_decision(context, item.name, decision)
            if decision is not None and not decision.continue_processing:
                context.stopped = True
                if decision.action is HookAction.REQUEST_APPROVAL:
                    context.approval_resume_index = index + 1
                session.emit("hook_short_circuit", hook=event,
                             middleware=item.name, action=decision.action.value,
                             reason=decision.reason)
                break
        session.emit("hook_finished", hook=event, stopped=context.stopped,
                     errors=list(context.errors), data=_audit_value(context.data))
        return context

    @staticmethod
    def _apply_decision(
        context: HookContext, middleware: str, decision: HookDecision
    ) -> None:
        action = decision.action
        if action is HookAction.MODIFY_REQUEST:
            context.data["request"] = decision.value
        elif action is HookAction.MODIFY_RESULT:
            context.data["result"] = decision.value
        elif action is HookAction.INJECT_CONTEXT:
            context.data.setdefault("context", []).append(str(decision.value or ""))
        elif action is HookAction.REQUEST_APPROVAL:
            context.data["approval"] = (
                decision.value if decision.value is not None else True
            )
        elif action is HookAction.TRIGGER_REPAIR:
            context.data["trigger_repair"] = True
        elif action is HookAction.BLOCK and decision.value:
            context.data.setdefault("context", []).append(str(decision.value))
        if action in {HookAction.BLOCK, HookAction.REQUEST_APPROVAL}:
            context.data["stop_reason"] = decision.reason
        context.session.emit(
            "hook_decision",
            hook=context.event,
            middleware=middleware,
            action=action.value,
            reason=decision.reason,
        )


@dataclass
class _Base:
    name: str
    order: int


class RepeatedActionGuard(_Base):
    def handle(self, context: HookContext) -> HookDecision | None:
        if context.event != "BeforeTool":
            return None
        name = context.payload.get("tool_name")
        arguments = context.payload.get("arguments", {})
        previous = [
            event for event in context.session.events
            if event.get("type") == "tool"
            and event.get("name") == name
            and event.get("arguments") == arguments
            and event.get("ok") is False
        ]
        if len(previous) >= 2:
            return HookDecision(
                HookAction.BLOCK,
                "identical action has already failed twice",
                (
                    "The same action failed twice. Change the arguments "
                    "or test a different diagnosis before retrying."
                ),
            )
        return None


def _audit_value(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _audit_value(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, dict):
        return {str(key): _audit_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_audit_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


__all__ = ["HookAction", "HookContext", "HookDecision", "HookPipeline",
           "Middleware", "RepeatedActionGuard"]
