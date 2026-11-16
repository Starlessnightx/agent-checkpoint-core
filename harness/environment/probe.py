from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from harness.executor.base import Executor


@dataclass(frozen=True)
class ProbeResult:
    name: str
    value: Any
    source: str
    freshness_seconds: float
    timeout_seconds: float
    error: str | None = None
    redacted: bool = False

    def dictionary(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value, "source": self.source,
                "freshness_seconds": self.freshness_seconds,
                "timeout_seconds": self.timeout_seconds, "error": self.error,
                "redacted": self.redacted}


class EnvProbe:
    def __init__(self, executor: Executor, workspace: Path, timeout_seconds: float = 2):
        self.executor, self.workspace = executor, workspace.resolve()
        self.timeout_seconds = timeout_seconds

    def run(self) -> tuple[ProbeResult, ...]:
        started = time.monotonic()
        results: list[ProbeResult] = []
        def add(name: str, value: Any, source: str, *, redacted: bool = False, error: str | None = None):
            results.append(ProbeResult(name, value, source, time.monotonic() - started,
                                       self.timeout_seconds, error, redacted))
        inspected = self.executor.inspect_environment()
        add("os", inspected.os, "executor.inspect_environment")
        add("arch", inspected.arch, "executor.inspect_environment")
        add("python", self.executor.which("python3") or self.executor.which("python"), "executor.which")
        add("node", self.executor.which("node"), "executor.which")
        add("git", self.executor.which("git"), "executor.which")
        add("package_manager.pip", self.executor.which("pip3") or self.executor.which("pip"), "executor.which")
        add("package_manager.npm", self.executor.which("npm"), "executor.which")
        safe_env = inspected.environment
        add("environment", {key: (value if key in {"PATH", "LANG", "SHELL"} else _presence(value))
                             for key, value in safe_env.items()}, "executor.environment", redacted=True)
        add("workspace", {"path": str(self.workspace), "exists": self.executor.exists(self.workspace),
                           "writable": inspected.workspace_writable,
                           "backend": inspected.backend}, "executor/filesystem")
        add("network", {"status": "unknown", "probe": "not_run"}, "policy", redacted=True)
        return tuple(results)


def _presence(value: str) -> str:
    return "set:" + hashlib.sha256(value.encode()).hexdigest()[:12] if value else "unset"
