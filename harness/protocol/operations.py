"""Python operation contracts aligned with Codex Op responsibilities, not its wire format."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import ClassVar


def identifier(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("identifier must be a nonempty string")


@dataclass(frozen=True, kw_only=True)
class Operation:
    session_id: str
    operation_id: str
    kind: ClassVar[str]

    def __post_init__(self):
        identifier(self.session_id)
        identifier(self.operation_id)
        if hasattr(self, "turn_id"):
            identifier(self.turn_id)

    def dictionary(self) -> dict:
        return {"version": 1, "kind": self.kind, **asdict(self)}


@dataclass(frozen=True, kw_only=True)
class UserTurn(Operation):
    kind: ClassVar[str] = "user_turn"
    turn_id: str
    text: str

    def __post_init__(self):
        super().__post_init__()
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("user input must be nonempty text")


@dataclass(frozen=True, kw_only=True)
class Steer(UserTurn):
    kind: ClassVar[str] = "steer"


@dataclass(frozen=True, kw_only=True)
class Interrupt(Operation):
    kind: ClassVar[str] = "interrupt"
    turn_id: str


@dataclass(frozen=True, kw_only=True)
class ApprovalResponse(Operation):
    kind: ClassVar[str] = "approval_response"
    turn_id: str
    request_id: str
    approved: bool

    def __post_init__(self):
        super().__post_init__()
        identifier(self.request_id)
        if type(self.approved) is not bool:
            raise ValueError("approval must be an explicit boolean")


@dataclass(frozen=True, kw_only=True)
class UserInputAnswer(Operation):
    kind: ClassVar[str] = "user_input_answer"
    turn_id: str
    request_id: str
    answers: tuple[tuple[str, str], ...]

    def __post_init__(self):
        super().__post_init__()
        identifier(self.request_id)
        if not isinstance(self.answers, tuple) or not self.answers:
            raise ValueError("answers must be a nonempty tuple")
        keys = set()
        for answer in self.answers:
            if not isinstance(answer, tuple) or len(answer) != 2:
                raise ValueError("answer must contain question ID and text")
            question, text = answer
            identifier(question)
            if question in keys or not isinstance(text, str):
                raise ValueError("duplicate question or non-text answer")
            keys.add(question)


@dataclass(frozen=True, kw_only=True)
class Compact(Operation):
    kind: ClassVar[str] = "compact"


@dataclass(frozen=True, kw_only=True)
class Shutdown(Operation):
    kind: ClassVar[str] = "shutdown"


def operation_from_dict(value: dict) -> Operation:
    data = dict(value)
    version = data.pop("version", None)
    if type(version) is not int or version != 1:
        raise ValueError("unsupported operation version")
    kinds = {item.kind: item for item in (
        UserTurn, Steer, Interrupt, ApprovalResponse, UserInputAnswer, Compact, Shutdown)}
    kind = data.pop("kind", None)
    if not isinstance(kind, str) or kind not in kinds:
        raise ValueError("unknown operation kind")
    if kind == UserInputAnswer.kind:
        answers = data.get("answers")
        if not isinstance(answers, (list, tuple)) or any(
            not isinstance(answer, (list, tuple)) for answer in answers
        ):
            raise ValueError("invalid answers payload")
        data["answers"] = tuple(tuple(answer) for answer in answers)
    try:
        return kinds[kind](**data)
    except TypeError as exc:
        raise ValueError("invalid operation fields") from exc
