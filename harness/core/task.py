from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class TaskContract:
    """Typed task boundary shared by execution and completion verification."""

    text: str
    mentioned_absolute_paths: tuple[str, ...] = ()
    required_artifacts: tuple[str, ...] = ()
    completion_conditions: tuple[str, ...] = ()
    verification_contract: dict[str, object] | None = None
    success_conditions: tuple[str, ...] = ()

    @classmethod
    def from_text(cls, text: str) -> "TaskContract":
        paths = sorted({
            match.rstrip(".,:;)'\"]")
            for match in re.findall(r"/app/[A-Za-z0-9_./-]+", text)
        })
        required = tuple(sorted(set(
            match for match in re.findall(
                r"(?:required artifact|artifact)\s*[:=]\s*([^\s,;]+)",
                text, re.IGNORECASE
            )
        )))
        explicit_outputs = re.findall(
            r"(?:write|save)\s+[^\n]{0,100}?\s+(?:to|at)\s+`?(/[A-Za-z0-9_./-]+)",
            text, re.IGNORECASE,
        )
        required = tuple(sorted(set(required) | {p.rstrip('.') for p in explicit_outputs}))
        conditions = tuple(line.strip().lstrip('- ').strip() for line in text.splitlines()
                           if any(operator in line for operator in ('≥', '≤', '>=', '<=')))
        return cls(text=text, mentioned_absolute_paths=tuple(paths),
                   required_artifacts=required,
                   completion_conditions=("fresh_verification_after_last_write",),
                   verification_contract={"required_checks": ()}, success_conditions=conditions)

    def dictionary(self) -> dict[str, object]:
        """Legacy shape retained for callers that persisted the original contract."""
        return {"mentioned_absolute_paths": list(self.mentioned_absolute_paths)}

    def full_dictionary(self) -> dict[str, object]:
        return {
            "text": self.text,
            "mentioned_absolute_paths": list(self.mentioned_absolute_paths),
            "required_artifacts": list(self.required_artifacts),
            "completion_conditions": list(self.completion_conditions),
            "verification_contract": self.verification_contract or {"required_checks": ()},
            "success_conditions": list(self.success_conditions),
        }


__all__ = ["TaskContract"]
