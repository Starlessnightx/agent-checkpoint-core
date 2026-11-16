from __future__ import annotations

from dataclasses import asdict, dataclass

from .json import ContractError, canonical, content_id, fields, identifier


@dataclass(frozen=True)
class Boundary:
    kind: str
    event_cursor: int
    sampling_index: int

    def __post_init__(self):
        if type(self.kind) is not str or self.kind not in {"initial", "before_model", "after_tools", "final_response"}:
            raise ContractError("unknown boundary")
        if any(type(v) is not int or v < 0 for v in (self.event_cursor, self.sampling_index)):
            raise ContractError("boundary cursors must be nonnegative integers")

    def dictionary(self) -> dict:
        return {"schema_version": 1, **asdict(self), "pending_action": "none"}

    @classmethod
    def load(cls, value: dict) -> Boundary:
        fields(value, {"schema_version", "kind", "event_cursor", "sampling_index", "pending_action"})
        if value["pending_action"] != "none":
            raise ContractError("pending action is not a recoverable boundary")
        return cls(value["kind"], value["event_cursor"], value["sampling_index"])

