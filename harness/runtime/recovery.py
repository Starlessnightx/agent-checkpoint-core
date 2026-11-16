from __future__ import annotations

import json
from collections import Counter
from typing import Any


class RecoveryGuard:
    def __init__(self, policy: dict[str, Any]):
        self.policy = policy
        self.signatures: Counter[str] = Counter()
        self.consecutive_failures = 0
        self.last_signature = ""
        self.incomplete_responses = 0

    def checkpoint_state(self) -> dict[str, Any]:
        return {"schema_version": 1, "signatures": dict(self.signatures),
                "consecutive_failures": self.consecutive_failures,
                "last_signature": self.last_signature,
                "incomplete_responses": self.incomplete_responses}

    def record_incomplete_response(self) -> bool:
        """Return whether another model response may be attempted."""
        self.incomplete_responses += 1
        return self.incomplete_responses < int(
            _get(self.policy, "max_incomplete_responses", 3)
        )

    def restore_checkpoint_state(self, value: dict[str, Any]) -> None:
        from harness.core.portable_state import copy_plain, require_fields, natural
        value = copy_plain(value)
        require_fields(value, self.checkpoint_state())
        if type(value['schema_version']) is not int or value['schema_version'] != 1:
            raise ValueError('unsupported recovery state')
        for key in ('consecutive_failures', 'incomplete_responses'):
            natural(value[key])
        if type(value['signatures']) is not dict or type(value['last_signature']) is not str:
            raise ValueError('invalid recovery signatures')
        for count in value['signatures'].values():
            natural(count)
        self.signatures = Counter(value['signatures'])
        self.consecutive_failures = value['consecutive_failures']
        self.incomplete_responses = value['incomplete_responses']
        self.last_signature = value['last_signature']

    def record_complete_response(self) -> None:
        self.incomplete_responses = 0

    def before(self, name: str, arguments: dict[str, Any], count: int) -> str | None:
        max_calls = _get(self.policy, "max_tool_calls")
        if max_calls is not None and count >= int(max_calls):
            return "tool-call budget exhausted"
        signature = json.dumps([name, arguments], sort_keys=True)
        # Time-varying process inspection is not an identical side-effect retry.
        inspection = name in {"process_status", "wait_process", "wait_for_tasks", "subagent_status", "collect_subagent_result"}
        if not inspection:
            self.signatures[signature] += 1
        if not inspection and self.signatures[signature] > int(
            _get(self.policy, "max_repeated_identical_calls")
        ):
            self.signatures[signature] = 0
            return self._message("repetition_message")
        if self.consecutive_failures >= int(
            _get(self.policy, "max_consecutive_failures")
        ):
            self.consecutive_failures = 0
            return self._message("failure_message")
        self.last_signature = signature
        return None

    def after(self, ok: bool, blocked: bool = False, retryable: bool = True) -> None:
        if blocked:
            return
        if not ok and not retryable:
            # Non-retryable failures (e.g., tool schema validation errors) are
            # not evidence of model stagnation; do not increment the counter.
            return
        self.consecutive_failures = 0 if ok else self.consecutive_failures + 1

    def _message(self, key: str) -> str:
        return str(
            _get(self.policy, key)
            or _get(self.policy, "stagnation_message")
            or "Choose a different action based on the latest evidence."
        )


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)
