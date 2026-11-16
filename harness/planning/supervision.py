from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class ReviewPolicy:
    """Cadence bounds, not execution or total model-call budgets."""

    initial_seconds: float = 5.0
    maximum_seconds: float = 300.0
    duration_fraction: float = 0.03

    def interval(self, elapsed: float, estimated: float | None = None) -> float:
        if estimated is not None and (not math.isfinite(estimated) or estimated <= 0):
            raise ValueError("estimated duration must be finite and positive")
        if elapsed < self.initial_seconds:
            return self.initial_seconds
        scale = max(self.initial_seconds, elapsed * 0.1,
                    (estimated or 0) * self.duration_fraction)
        if estimated is not None and elapsed >= estimated:
            # An overdue estimate calls for closer review, not killing work.
            scale = min(scale, max(self.initial_seconds, estimated * 0.01))
        return min(self.maximum_seconds, scale)
