"""Portable handoff from a Harness adapter to the storage pipeline."""
from dataclasses import dataclass
from pathlib import Path
import re

from .models import Boundary
from .json import ContractError


@dataclass(frozen=True)
class FrozenAgentData:
    adapter: str
    adapter_version: str
    boundary: Boundary
    components: tuple[tuple[str, bytes], ...]
    archives: tuple[tuple[str, Path], ...]

    def __post_init__(self):
        required = {"conversation", "model_view", "context", "task", "plan", "tools", "budget", "usage",
                    "pending_inputs", "completed_children", "recovery", "api_audit", "raw_events", "configuration"}
        if (not isinstance(self.boundary, Boundary)
                or any(type(value) is not str or not 1 <= len(value) <= 128 for value in (self.adapter, self.adapter_version))):
            raise ContractError("invalid frozen Agent identity or boundary")
        if (type(self.components) is not tuple
                or any(type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
                       or type(item[1]) is not bytes for item in self.components)
                or len(self.components) != len(required) or {name for name, _ in self.components} != required):
            raise ContractError("frozen Agent component set is incomplete or duplicated")
        if type(self.archives) is not tuple:
            raise ContractError("archives must be immutable entries")
        labels = set()
        for item in self.archives:
            if (type(item) is not tuple or len(item) != 2 or type(item[0]) is not str
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", item[0]) or item[0] in labels
                    or not isinstance(item[1], Path) or not item[1].is_absolute()):
                raise ContractError("invalid or duplicated archive entry")
            labels.add(item[0])

