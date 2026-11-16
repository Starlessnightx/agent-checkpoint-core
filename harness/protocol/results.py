"""Turn lifecycle results do not claim task verification or benchmark success."""
from dataclasses import asdict, dataclass
from enum import Enum

from .operations import identifier


class TurnOutcome(str, Enum):
    COMPLETED = "completed"
    INTERRUPTED = "interrupted"
    REPLACED = "replaced"
    BUDGET_LIMITED = "budget_limited"
    ERROR = "error"


@dataclass(frozen=True)
class TurnResult:
    session_id: str
    turn_id: str
    outcome: TurnOutcome
    last_agent_message: str | None = None
    error: str | None = None

    def __post_init__(self):
        identifier(self.session_id)
        identifier(self.turn_id)
        if not isinstance(self.outcome, TurnOutcome):
            raise ValueError("outcome must be a TurnOutcome")
        if self.last_agent_message is not None and not isinstance(self.last_agent_message, str):
            raise ValueError("last agent message must be text")
        if self.error is not None and not isinstance(self.error, str):
            raise ValueError("error must be text")
        if self.outcome is TurnOutcome.ERROR and not self.error:
            raise ValueError("error outcome requires error detail")
        if self.outcome is not TurnOutcome.ERROR and self.error is not None:
            raise ValueError("non-error outcome cannot carry an error")

    def dictionary(self) -> dict:
        return {**asdict(self), "outcome": self.outcome.value}
