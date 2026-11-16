from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from safety.boundaries import ensure_within

from harness.tools.base import ToolContext, ToolRequest


@dataclass(frozen=True)
class SandboxResult:
    allowed: bool
    reason: str

    def dictionary(self) -> dict[str, Any]:
        return {"allowed": self.allowed, "reason": self.reason}


class Sandbox:
    """Reject path and working-directory escapes before tool execution."""

    _PATH_KEYS = ("path", "cwd", "root", "directory", "workdir")

    def check(self, request: ToolRequest, context: ToolContext) -> SandboxResult:
        for key in self._PATH_KEYS:
            if key not in request.arguments or request.arguments[key] in (None, ""):
                continue
            raw = Path(str(request.arguments[key]))
            candidate = raw if raw.is_absolute() else context.workspace / raw
            try:
                ensure_within(candidate, context.workspace)
            except PermissionError as exc:
                return SandboxResult(False, f"{key}: {exc}")
        return SandboxResult(True, "paths remain within workspace")
