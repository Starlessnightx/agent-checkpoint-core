from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from typing import Any
import json
from datetime import datetime, timezone
from harness.executor.base import Executor

from safety.audit import append_event


@dataclass(frozen=True)
class RuntimeEvent:
    """Typed envelope with a legacy-compatible dictionary representation."""

    event_id: str
    event_type: str
    payload: dict[str, Any]

    def dictionary(self) -> dict[str, Any]:
        return {"event_id": self.event_id, "type": self.event_type, **self.payload}


class EventRecorder:
    def __init__(self, path: Path, *, executor: Executor | None = None, offset: int = 0):
        self.path = path
        self.events: list[dict[str, Any]] = []
        self._lock = RLock()
        self.executor = executor
        self.offset = offset
        self._inherited: tuple[dict[str, Any], ...] = ()

    @property
    def history(self) -> list[dict[str, Any]]:
        return [*self._inherited, *self.events] if self._inherited else self.events

    @property
    def cursor(self) -> int:
        return self.offset + len(self.events)

    def inherit_prefix(self, events: list[dict[str, Any]]) -> None:
        """Attach copied historical data to a fresh recorder; never rewrite audit.

        The caller retains the immutable prefix in its archive and records its
        source in the new branch's lifecycle event. Local audit remains a suffix.
        """
        from .portable_state import copy_plain
        value = copy_plain(events)
        if type(value) is not list:
            raise ValueError('event prefix must be a list')
        for i, event in enumerate(value, 1):
            if (type(event) is not dict or event.get('event_id') != f'E-{i:06d}'
                    or type(event.get('type')) is not str):
                raise ValueError('event prefix identities differ')
        with self._lock:
            if self.events or self.offset or self._inherited:
                raise RuntimeError('event prefix requires a fresh recorder')
            self._inherited, self.offset = tuple(value), len(value)

    def initialize(self) -> None:
        if self.executor is not None:
            if self.executor.exists(self.path):
                raise FileExistsError("refusing to overwrite an existing session audit")
            self.executor.write_file(self.path, b"")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("", encoding="utf-8")

    def emit(self, event_type: str, **payload: Any) -> dict[str, Any]:
        # Legacy worker and submission consumer may both record events. Keep
        # event IDs, memory order and persisted order in the same critical section.
        with self._lock:
            event = RuntimeEvent(
                event_id=f"E-{self.offset + len(self.events) + 1:06d}",
                event_type=event_type,
                payload=payload,
            ).dictionary()
            if self.executor is not None:
                event["time"] = datetime.now(timezone.utc).isoformat()
            if self.executor is None:
                append_event(self.path, event)
            else:
                self.executor.append_file(self.path, (json.dumps(event, ensure_ascii=False) + "\n").encode())
            self.events.append(event)
            return event



__all__ = ["EventRecorder", "RuntimeEvent"]
