from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .tools.base import ToolResult


@dataclass
class AgentResult:
    text: str
    verified: bool
    reasons: tuple[str, ...]
    events: list[dict[str, Any]] = field(default_factory=list)


class ChatModel(Protocol):
    def complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        temperature: float = 0,
        cache_key: str | None = None,
    ) -> dict[str, Any]: ...
