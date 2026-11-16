from __future__ import annotations

import asyncio
from typing import Callable, Protocol, TYPE_CHECKING, TypeVar

from harness.protocol import TurnResult

if TYPE_CHECKING:
    from harness.core.session import Session
    from harness.core.submission import TurnControl


class TurnDriver(Protocol):
    async def run_turn(self, session: Session, control: TurnControl) -> TurnResult: ...


T = TypeVar("T")


async def join_on_cancel(task: asyncio.Task[T], cancel: Callable[[], None]) -> T:
    """Never abandon work still using Session resources, including on re-cancel."""
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        cancel()
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Retrieve any worker exception, but preserve caller cancellation.
        await asyncio.gather(task, return_exceptions=True)
        raise
