"""Optional public extension point at fully joined model/tool boundaries.

The kernel owns scheduling. An extension may await storage here, but cannot
create a safe point in the middle of a running worker or pending tool group.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal, Protocol, Callable

if TYPE_CHECKING:
    from .session import Session
    from .submission import TurnControl
    from .turn_context import TurnContext


@dataclass(frozen=True)
class RuntimeBoundary:
    kind: Literal["initial", "before_model", "after_tools", "final_response"]
    turn_id: str
    sampling_step: int
    event_cursor: int
    conversation_cursor: int


class BoundaryHandler(Protocol):
    async def at_boundary(self, session: Session, boundary: RuntimeBoundary,
                          context: TurnContext, control: TurnControl) -> None: ...


@dataclass(frozen=True)
class RestoredTurnPoint:
    """Data-only scheduling position, installed explicitly by a trusted adapter."""
    kind: Literal['initial', 'before_model', 'after_tools', 'final_response']
    steps: int
    sampling_index: int
    unsupported_output_notices: int = 0

    def __post_init__(self):
        if self.kind not in {'initial', 'before_model', 'after_tools', 'final_response'}:
            raise ValueError('unknown restored turn boundary')
        if any(type(v) is not int or v < 0 for v in
               (self.steps, self.sampling_index, self.unsupported_output_notices)):
            raise ValueError('restored turn counters must be nonnegative integers')


BoundaryFactory = Callable[["Session"], BoundaryHandler]
