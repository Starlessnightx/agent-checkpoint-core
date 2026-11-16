from __future__ import annotations

from typing import Any, TYPE_CHECKING
if TYPE_CHECKING:
    from .session import Session
    from harness.types import ChatModel

from .hooks import HookContext

def _text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", "")) for part in content if isinstance(part, dict)
        )
    return str(content or "")

def _inject_hook_context(
    session: Session,
    context: HookContext | None,
    turn: int,
    phase: str,
    *,
    deferred_messages: list[dict[str, Any]] | None = None,
) -> None:
    if context is None:
        return
    for value in context.data.get("context", ()):
        text = str(value).strip()
        if not text:
            continue
        message = {"role": "system", "content": text}
        if deferred_messages is None:
            session.messages.append(message)
        else:
            deferred_messages.append(message)
        session.emit(
            "hook_context_injected",
            hook=context.event,
            turn=turn,
            phase=phase,
            content_preview=text[:400],
            deferred_until_tool_batch_complete=deferred_messages is not None,
        )

def _last_usage(model: ChatModel) -> dict[str, int]:
    reader = getattr(model, "last_usage", None)
    value = reader() if callable(reader) else {}
    return value if isinstance(value, dict) else {}

def _usage_snapshot(model: ChatModel) -> dict[str, int]:
    reader = getattr(model, "usage_snapshot", None)
    value = reader() if callable(reader) else {}
    return value if isinstance(value, dict) else {}

def _policy_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
