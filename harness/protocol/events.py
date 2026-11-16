"""Frontend event contracts; request matching grants no execution permission."""
from dataclasses import asdict, dataclass
from enum import Enum

from .operations import ApprovalResponse, UserInputAnswer, identifier


class EventKind(str, Enum):
    SUBMISSION_RECEIVED = "submission_received"
    SUBMISSION_ACCEPTED = "submission_accepted"
    SUBMISSION_REJECTED = "submission_rejected"
    INPUT_STEERED = "input_steered"
    INPUT_UNCONSUMED = "input_unconsumed"
    REQUEST_RESOLVED = "request_resolved"
    REQUEST_CANCELLED = "request_cancelled"
    SESSION_SHUTDOWN = "session_shutdown"
    TURN_STARTED = "turn_started"
    ASSISTANT_MESSAGE = "assistant_message"
    TOOL_STARTED = "tool_started"
    TOOL_FINISHED = "tool_finished"
    APPROVAL_REQUESTED = "approval_requested"
    USER_INPUT_REQUESTED = "user_input_requested"
    TURN_COMPLETED = "turn_completed"
    TURN_ABORTED = "turn_aborted"


@dataclass(frozen=True)
class RequestRef:
    session_id: str
    turn_id: str
    request_id: str
    kind: EventKind

    def __post_init__(self):
        for value in (self.session_id, self.turn_id, self.request_id):
            identifier(value)
        if not isinstance(self.kind, EventKind) or self.kind not in (EventKind.APPROVAL_REQUESTED, EventKind.USER_INPUT_REQUESTED):
            raise ValueError("request kind must require a frontend reply")

    def validate_reply(self, reply: ApprovalResponse | UserInputAnswer) -> None:
        expected = ApprovalResponse if self.kind is EventKind.APPROVAL_REQUESTED else UserInputAnswer
        if not isinstance(reply, expected) or (
            reply.session_id, reply.turn_id, reply.request_id
        ) != (self.session_id, self.turn_id, self.request_id):
            raise ValueError("reply does not match request kind/session/turn/ID")


@dataclass(frozen=True)
class ProtocolEvent:
    event_id: str
    session_id: str
    turn_id: str
    kind: EventKind
    request_id: str | None = None
    text: str | None = None

    def __post_init__(self):
        for value in (self.event_id, self.session_id, self.turn_id):
            identifier(value)
        if not isinstance(self.kind, EventKind):
            raise ValueError("event kind must be an EventKind")
        if self.request_id is not None:
            identifier(self.request_id)
        if self.kind in (EventKind.APPROVAL_REQUESTED, EventKind.USER_INPUT_REQUESTED):
            identifier(self.request_id)
        if self.text is not None and not isinstance(self.text, str):
            raise ValueError("event text must be a string")

    def dictionary(self) -> dict:
        return {"version": 1, **asdict(self), "kind": self.kind.value}
