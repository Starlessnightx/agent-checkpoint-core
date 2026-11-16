"""Counters/cancellation for one protocol turn, distinct from one sampling request."""
from dataclasses import dataclass, field
from threading import Event


@dataclass
class TurnContext:
    turn_id: str
    steps: int = 0
    unsupported_output_notices: int = 0
    worker_cancelled: Event = field(default_factory=Event)
