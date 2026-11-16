"""Small, dependency-free command policy helpers for foreground shell tools."""
from __future__ import annotations

import re


_SLEEP = re.compile(
    r"(?<![\w./-])sleep\s+(\d+(?:\.\d+)?)\s*([smhd]?)",
    re.IGNORECASE,
)
_UNIT_SECONDS = {"": 1.0, "s": 1.0, "m": 60.0, "h": 3600.0, "d": 86400.0}
DEFAULT_MAX_SLEEP_SECONDS = 30.0


def long_sleep_seconds(
    command: str,
    *,
    max_seconds: float = DEFAULT_MAX_SLEEP_SECONDS,
) -> float | None:
    """Return the first excessive sleep duration in a shell command.

    This is intentionally a conservative textual check for foreground shell
    tools. It does not rewrite the command and does not inspect arbitrary
    programs that may sleep internally.
    """
    for match in _SLEEP.finditer(command):
        try:
            seconds = float(match.group(1)) * _UNIT_SECONDS[match.group(2).lower()]
        except (KeyError, ValueError):
            continue
        if seconds > max_seconds:
            return seconds
    return None
