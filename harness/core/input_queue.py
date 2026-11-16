"""Session submissions and turn-local inputs, confined to one asyncio loop."""
from __future__ import annotations

import asyncio
from collections import deque
from contextlib import contextmanager

from harness.protocol import (
    ApprovalResponse, Compact, Interrupt, Operation, Shutdown, Steer,
    UserInputAnswer, UserTurn,
)
from harness.protocol.operations import identifier


class InputQueue:
    def __init__(self, session_id: str):
        identifier(session_id)
        self.session_id = session_id
        self._queue: asyncio.Queue[Operation | None] = asyncio.Queue()
        self._seen: set[str] = set()
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None

    def _check_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("input queue belongs to another event loop")

    def submit(self, operation: Operation) -> None:
        self.validate(operation)
        self._seen.add(operation.operation_id)
        self._queue.put_nowait(operation)

    def validate(self, operation: Operation) -> None:
        self._check_loop()
        if type(operation) not in (UserTurn, Steer, Interrupt, ApprovalResponse,
                                   UserInputAnswer, Compact, Shutdown):
            raise ValueError("unsupported operation type")
        if operation.session_id != self.session_id:
            raise ValueError("operation belongs to another session")
        if self._closed:
            raise RuntimeError("session input is closed")
        if operation.operation_id in self._seen:
            raise ValueError("operation ID already submitted")

    async def receive(self) -> Operation | None:
        self._check_loop()
        if self._closed and self._queue.empty():
            return None
        return await self._queue.get()

    def close(self) -> tuple[Operation, ...]:
        self._check_loop()
        self._closed = True
        discarded = []
        while not self._queue.empty():
            operation = self._queue.get_nowait()
            if operation is not None:
                discarded.append(operation)
        self._queue.put_nowait(None)
        return tuple(discarded)


class TurnInputQueue:
    def __init__(self):
        self._items: deque[Steer] = deque()
        self._changed = asyncio.Event()
        self._held: deque[Steer] | None = None

    def put(self, operation: Steer) -> None:
        if self._held is not None:
            self._held.append(operation)
        else:
            self._items.append(operation)
            self._changed.set()

    @contextmanager
    def freeze(self):
        """Seal a snapshot prefix; newly arriving steering stays on this branch."""
        if self._held is not None:
            raise RuntimeError("turn inputs are already frozen")
        self._held = deque()
        try:
            yield self.snapshot()
        finally:
            self._items.extend(self._held)
            self._held = None
            if self._items:
                self._changed.set()

    def snapshot(self) -> tuple[Steer, ...]:
        return tuple(self._items)

    def drain(self) -> tuple[Steer, ...]:
        if self._held is not None:
            raise RuntimeError("cannot consume input while its snapshot prefix is frozen")
        items = tuple(self._items)
        self._items.clear()
        self._changed.clear()
        return items

    @property
    def has_pending_input(self) -> bool:
        return bool(self._items)

    async def wait(self) -> None:
        await self._changed.wait()
