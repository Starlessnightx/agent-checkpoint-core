"""Single-use frontend replies. This registry does not grant tool permissions."""
from __future__ import annotations

import asyncio

from harness.protocol import ApprovalResponse, RequestRef, UserInputAnswer

Reply = ApprovalResponse | UserInputAnswer


class PendingRequests:
    def __init__(self, session_id: str):
        self.session_id = session_id
        self._pending: dict[str, tuple[RequestRef, asyncio.Future[Reply]]] = {}
        self._seen: set[str] = set()
        self._answer_ids: dict[str, frozenset[str]] = {}

    @property
    def pending_count(self) -> int:
        return len(self._pending)

    def register(self, ref: RequestRef, *, answer_ids: frozenset[str] | None = None) -> asyncio.Future[Reply]:
        if ref.session_id != self.session_id:
            raise ValueError("request belongs to another session")
        if ref.request_id in self._seen:
            raise ValueError("request ID already used")
        future: asyncio.Future[Reply] = asyncio.get_running_loop().create_future()
        self._seen.add(ref.request_id)
        self._pending[ref.request_id] = (ref, future)
        if answer_ids is not None:
            self._answer_ids[ref.request_id] = answer_ids
        return future

    def resolve(self, reply: Reply) -> None:
        entry = self._pending.get(reply.request_id)
        if entry is None:
            raise ValueError("request is unknown, resolved or closed")
        ref, future = entry
        ref.validate_reply(reply)
        expected = self._answer_ids.get(reply.request_id)
        if expected is not None and isinstance(reply, UserInputAnswer) and frozenset(
            key for key, _ in reply.answers
        ) != expected:
            raise ValueError("answer IDs must match the requested questions")
        if future.done():
            raise ValueError("request is already resolved or cancelled")
        # No await between matching and removal: two replies cannot both win.
        self._pending.pop(reply.request_id)
        self._answer_ids.pop(reply.request_id, None)
        future.set_result(reply)

    def cancel(self, request_id: str) -> RequestRef | None:
        entry = self._pending.pop(request_id, None)
        self._answer_ids.pop(request_id, None)
        if entry is None:
            return None
        ref, future = entry
        if not future.done():
            future.cancel()
        return ref

    def cancel_turn(self, turn_id: str) -> tuple[RequestRef, ...]:
        cancelled = []
        for ref, _ in tuple(self._pending.values()):
            if ref.turn_id == turn_id:
                self.cancel(ref.request_id)
                cancelled.append(ref)
        return tuple(cancelled)
