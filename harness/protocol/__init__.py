"""Codex-aligned protocol contracts, introduced before Runtime migration."""
from .operations import (
    Operation, UserTurn, Steer, Interrupt, ApprovalResponse, UserInputAnswer,
    Compact, Shutdown, operation_from_dict,
)
from .events import EventKind, ProtocolEvent, RequestRef
from .results import TurnOutcome, TurnResult

__all__ = [
    "Operation", "UserTurn", "Steer", "Interrupt", "ApprovalResponse",
    "UserInputAnswer", "Compact", "Shutdown", "operation_from_dict",
    "EventKind", "ProtocolEvent", "RequestRef", "TurnOutcome", "TurnResult",
]
