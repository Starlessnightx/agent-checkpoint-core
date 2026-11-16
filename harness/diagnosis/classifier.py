from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any


class FailureCategory(str, Enum):
    MISSING_BINARY = "MissingBinary"
    MISSING_PACKAGE = "MissingPackage"
    INTERPRETER_MISMATCH = "InterpreterMismatch"
    WRONG_WORKING_DIRECTORY = "WrongWorkingDirectory"
    MISSING_DEPENDENCY = "MissingDependency"
    PERMISSION = "PermissionDenied"
    TIMEOUT = "Timeout"
    NETWORK = "NetworkFailure"
    DISK_FULL = "DiskFull"
    OUT_OF_MEMORY = "OutOfMemory"
    TEST_FAILURE = "TestFailure"
    COMMAND_FAILURE = "CommandFailure"
    UNKNOWN = "Unknown"


@dataclass(frozen=True)
class FailureDiagnosis:
    category: FailureCategory
    signature: str
    evidence: tuple[dict[str, Any], ...]
    confidence: float
    alternatives: tuple[FailureCategory, ...] = ()
    next_probe: str | None = None
    deterministic: bool = True

    def dictionary(self) -> dict[str, Any]:
        return {"category": self.category.value, "signature": self.signature,
                "evidence": list(self.evidence), "confidence": self.confidence,
                "alternatives": [item.value for item in self.alternatives],
                "next_probe": self.next_probe, "deterministic": self.deterministic}


class FailureClassifier:
    """Signature → deterministic classifier → targeted constraints; no installs."""

    def diagnose(self, failure: Any, environment: Any = None) -> FailureDiagnosis:
        output = _output(failure)
        lowered = output.casefold()
        signature = _signature(lowered)
        evidence: list[dict[str, Any]] = [{"source": "failure_output", "excerpt": output[:1000]}]
        if "bad interpreter" in lowered or "requires python" in lowered:
            evidence.append({"source": "signature", "matched": FailureCategory.INTERPRETER_MISMATCH.value})
            return FailureDiagnosis(
                FailureCategory.INTERPRETER_MISMATCH, signature, tuple(evidence), 0.9
            )
        if "can't open file" in lowered or "cannot open file" in lowered:
            evidence.append({"source": "signature", "matched": FailureCategory.WRONG_WORKING_DIRECTORY.value})
            return FailureDiagnosis(
                FailureCategory.WRONG_WORKING_DIRECTORY, signature, tuple(evidence), 0.85
            )
        if "modulenotfounderror" in lowered or "no module named" in lowered:
            package = _module_name(output)
            evidence.append({"source": "module_name", "value": package})
            category = self._module_category(package, environment, evidence)
            return FailureDiagnosis(category, signature, tuple(evidence),
                                    0.95 if category is not FailureCategory.UNKNOWN else 0.45,
                                    (FailureCategory.MISSING_PACKAGE, FailureCategory.INTERPRETER_MISMATCH)
                                    if category is FailureCategory.UNKNOWN else (),
                                    "inspect active interpreter, package owner, and sys.path"
                                    if category is FailureCategory.UNKNOWN else None)
        patterns = [
            (FailureCategory.MISSING_BINARY, ("command not found", "executable not found")),
            (FailureCategory.TIMEOUT, ("timed out", "timeout")),
            (FailureCategory.PERMISSION, ("permission denied", "operation not permitted")),
            (FailureCategory.DISK_FULL, ("no space left",)),
            (FailureCategory.OUT_OF_MEMORY, ("out of memory", "oom killed")),
            (FailureCategory.NETWORK, ("connection reset", "temporary failure", "network is unreachable")),
            (FailureCategory.MISSING_DEPENDENCY, ("no such file or directory",)),
            (FailureCategory.TEST_FAILURE, ("assertionerror", "failed", "failures=")),
        ]
        for category, needles in patterns:
            if any(needle in lowered for needle in needles):
                evidence.append({"source": "signature", "matched": category.value})
                return FailureDiagnosis(category, signature, tuple(evidence), 0.9)
        return FailureDiagnosis(FailureCategory.UNKNOWN, signature, tuple(evidence), 0.2,
                                deterministic=True, next_probe="collect targeted command and environment evidence")

    def _module_category(self, package: str, environment: Any, evidence: list[dict[str, Any]]) -> FailureCategory:
        values = environment if isinstance(environment, dict) else getattr(environment, "__dict__", {})
        packages = values.get("packages", {}) if isinstance(values, dict) else {}
        if package and packages.get(package) is True:
            evidence.append({"source": "package_inventory", "installed": True})
            return FailureCategory.INTERPRETER_MISMATCH
        if package and packages.get(package) is False:
            evidence.append({"source": "package_inventory", "installed": False})
            return FailureCategory.MISSING_PACKAGE
        return FailureCategory.UNKNOWN


def _output(value: Any) -> str:
    if isinstance(value, dict):
        return " ".join(str(value.get(key, "")) for key in ("summary", "output", "stderr", "message"))
    return str(value)


def _module_name(output: str) -> str:
    match = re.search(r"(?:No module named|ModuleNotFoundError:\s*No module named)\s*['\"]?([A-Za-z0-9_.-]+)", output, re.IGNORECASE)
    return match.group(1) if match else ""


def _signature(output: str) -> str:
    lowered = re.sub(r"\d+", "#", output.casefold())
    return re.sub(r"\s+", " ", lowered).strip()[:240]
