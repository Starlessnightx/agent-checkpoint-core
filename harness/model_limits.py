from __future__ import annotations

from dataclasses import dataclass
from math import isfinite
import re


class ModelContextLengthError(RuntimeError):
    """The provider rejected a request because its context is too large."""

    @property
    def token_gap(self) -> int | None:
        text = str(self).replace(",", "")
        limit = re.search(r"(?:maximum context length is|context limit[: =]+|max(?:imum)?[_ ]tokens[: =]+)\s*(\d+)", text, re.I)
        requested = re.search(r"(?:requested|request(?:ed)?[_ ]tokens[: =]+|resulted in)\s*(\d+)", text, re.I)
        if limit and requested:
            return max(1, int(requested.group(1)) - int(limit.group(1)))
        return None


@dataclass(frozen=True)
class ModelLimits:
    """Verified per-request limits for the selected provider and model."""

    context_window_tokens: int | None = None
    output_tokens: int | None = None
    output_reserve_tokens: int | None = None
    input_capacity_tokens: int | None = None

    def __post_init__(self) -> None:
        if self.context_window_tokens is not None and self.context_window_tokens < 1:
            raise ValueError("context_window_tokens must be positive")
        if self.output_tokens is not None and self.output_tokens < 1:
            raise ValueError("output_tokens must be positive")
        if self.output_reserve_tokens is not None and self.output_reserve_tokens < 1:
            raise ValueError("output_reserve_tokens must be positive")
        if self.input_capacity_tokens is not None and self.input_capacity_tokens < 1:
            raise ValueError("input_capacity_tokens must be positive")
        for reserve in (self.output_reserve_tokens, self.output_tokens):
            if (
                self.context_window_tokens is not None
                and reserve is not None
                and reserve >= self.context_window_tokens
            ):
                raise ValueError("output reserve must be smaller than context_window_tokens")
        if (
            self.context_window_tokens is not None
            and self.input_capacity_tokens is not None
            and self.input_capacity_tokens > self.context_window_tokens
        ):
            raise ValueError("input_capacity_tokens cannot exceed context_window_tokens")

    @property
    def input_capacity(self) -> int | None:
        if self.context_window_tokens is None:
            return self.input_capacity_tokens
        reserve = (
            self.output_reserve_tokens or self.output_tokens
            or max(1, self.context_window_tokens // 10)
        )
        capacity = self.context_window_tokens - reserve
        if self.input_capacity_tokens is not None:
            capacity = min(capacity, self.input_capacity_tokens)
        return capacity


@dataclass(frozen=True)
class ModelPricing:
    """Per-million-token USD prices supplied by the selected provider."""

    input_usd: float
    output_usd: float
    cached_input_usd: float | None = None

    def __post_init__(self) -> None:
        if any(
            not isfinite(price) or price < 0
            for price in (self.input_usd, self.output_usd)
        ):
            raise ValueError("model prices cannot be negative")
        if self.cached_input_usd is not None and (
            not isfinite(self.cached_input_usd) or self.cached_input_usd < 0
        ):
            raise ValueError("model prices cannot be negative")

    def request_cost(self, usage: dict[str, int]) -> float:
        input_tokens = max(0, int(usage.get("input_tokens", 0)))
        cached = min(input_tokens, max(0, int(usage.get("cache_read_tokens", 0))))
        output = max(0, int(usage.get("output_tokens", 0)))
        cache_price = (
            self.input_usd if self.cached_input_usd is None
            else self.cached_input_usd
        )
        return (
            (input_tokens - cached) * self.input_usd
            + cached * cache_price
            + output * self.output_usd
        ) / 1_000_000
