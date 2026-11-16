"""Request-prefix opportunities, not provider cache hits or billing estimates."""
from hashlib import sha256
import json
from typing import Any


def _digest(value: Any) -> str:
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                             separators=(",", ":"), default=str).encode()).hexdigest()


class PrefixObserver:
    """Keep digests only: no second conversation, model calls or disk effects."""

    def __init__(self):
        self._messages: tuple[str, ...] = ()
        self._schemas: str | None = None

    def checkpoint_state(self) -> dict[str, Any]:
        return {"messages": list(self._messages), "schemas": self._schemas}

    def restore_checkpoint_state(self, value: dict[str, Any]) -> None:
        from harness.core.portable_state import copy_plain, require_fields, strings
        import re
        value = copy_plain(value)
        require_fields(value, {'messages', 'schemas'})
        strings(value['messages'])
        if any(not re.fullmatch(r'[0-9a-f]{64}', v) for v in value['messages']):
            raise ValueError('invalid message prefix digest')
        if value['schemas'] is not None and (type(value['schemas']) is not str or
                not re.fullmatch(r'[0-9a-f]{64}', value['schemas'])):
            raise ValueError('invalid tool schema digest')
        self._messages, self._schemas = tuple(value['messages']), value['schemas']

    def observe(
        self, messages: list[dict[str, Any]], schemas: list[dict[str, Any]] | None
    ) -> dict[str, Any]:
        schemas_hash = _digest(schemas or [])
        current = tuple(_digest(message) for message in messages)
        same_schemas = self._schemas == schemas_hash
        shared = 0
        if same_schemas:
            for before, after in zip(self._messages, current):
                if before != after:
                    break
                shared += 1
        result = {"shared_prefix_messages": shared,
                  "previous_message_count": len(self._messages),
                  "tool_schemas_changed": self._schemas is not None and not same_schemas,
                  "measurement": "local_message_prefix_not_provider_cache_hit"}
        self._messages, self._schemas = current, schemas_hash
        return result
