from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable, Literal

from .base import Executor, ProcessStatus


@dataclass(frozen=True)
class ProcessWaitResult:
    status: ProcessStatus | None
    outcome: Literal["completed", "timeout", "interrupted", "not_found"]
    elapsed_seconds: float
    checks: int

    def dictionary(self) -> dict[str, object]:
        return {
            "outcome": self.outcome, "elapsed_seconds": self.elapsed_seconds,
            "checks": self.checks,
            "handle": self.status.handle if self.status else None,
            "running": self.status.running if self.status else False,
            "return_code": self.status.return_code if self.status else None,
        }


def wait_for_process(
    executor: Executor,
    handle: str,
    *,
    timeout_seconds: float | None = None,
    max_poll_interval_seconds: float = 1.0,
    interrupted: Callable[[], bool] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> ProcessWaitResult:
    """Executor-only inspection; no model, Session, or tool dispatch dependency.

    Log updates do not end completion waiting. A deadline does not kill the
    process. Check interruption at least once per second, even for long jobs.
    """
    if timeout_seconds is not None and (
        not math.isfinite(timeout_seconds) or timeout_seconds < 0
    ):
        raise ValueError("timeout_seconds must be finite and nonnegative")
    if not math.isfinite(max_poll_interval_seconds) or max_poll_interval_seconds <= 0:
        raise ValueError("poll interval must be finite and positive")
    started = clock()
    checks = 0
    delay = 0.1
    while True:
        status = executor.process_status(handle)
        checks += 1
        elapsed = clock() - started
        if status is None:
            return ProcessWaitResult(None, "not_found", elapsed, checks)
        if interrupted is not None and interrupted():
            return ProcessWaitResult(status, "interrupted", elapsed, checks)
        if not status.running:
            return ProcessWaitResult(status, "completed", elapsed, checks)
        if timeout_seconds is not None and elapsed >= timeout_seconds:
            return ProcessWaitResult(status, "timeout", elapsed, checks)
        remaining = delay if timeout_seconds is None else min(
            delay, timeout_seconds - elapsed
        )
        if interrupted is None:
            sleep(remaining)
        else:
            until = clock() + remaining
            while clock() < until:
                sleep(min(1.0, until - clock()))
                if interrupted():
                    return ProcessWaitResult(status, "interrupted", clock() - started, checks)
        delay = min(max_poll_interval_seconds, delay * 2)
