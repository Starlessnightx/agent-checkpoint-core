from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .state import EnvironmentState


@dataclass(frozen=True)
class DoctorReport:
    healthy: bool
    issues: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()

    def dictionary(self) -> dict[str, Any]:
        return {"healthy": self.healthy, "issues": list(self.issues),
                "recommendations": list(self.recommendations)}


class Doctor:
    def check(self, state: EnvironmentState) -> DoctorReport:
        issues: list[str] = []
        if not state.workspace.get("exists"):
            issues.append("workspace does not exist")
        if not state.workspace.get("writable"):
            issues.append("workspace is not writable")
        if not state.executables.get("python"):
            issues.append("python executable is unavailable")
        recommendations = tuple(f"inspect: {issue}" for issue in issues)
        return DoctorReport(not issues, tuple(issues), recommendations)
