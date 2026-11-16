"""Checkpoint worthiness policy, separate from Runtime quiescence checks."""
from dataclasses import dataclass
import math
import time


@dataclass(frozen=True)
class CheckpointPolicyConfig:
    minimum_interval_seconds: float = 120
    fallback_interval_seconds: float = 900

    def __post_init__(self):
        for value in (self.minimum_interval_seconds, self.fallback_interval_seconds):
            if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
                raise ValueError('checkpoint intervals must be positive finite seconds')
        if self.fallback_interval_seconds < self.minimum_interval_seconds:
            raise ValueError('fallback interval must be at least the minimum interval')

    @classmethod
    def load(cls, value=None):
        if value is None:
            return cls()
        if type(value) is not dict or set(value) != {'minimum_interval_seconds', 'fallback_interval_seconds'}:
            raise ValueError('invalid checkpoint trigger configuration')
        return cls(**value)


@dataclass(frozen=True)
class SaveDecision:
    save: bool
    reason: str


class CheckpointPolicy:
    def __init__(self, config=None, *, clock=time.monotonic):
        self.config = config or CheckpointPolicyConfig()
        self.clock = clock
        self.last_saved = clock()
        self.observed_cursor = None
        self.dirty = False
        self.progress = False
        self.pending = None

    def observe(self, events, *, cursor, inherited_cursor=0):
        start = inherited_cursor if self.observed_cursor is None else self.observed_cursor
        if cursor < start or cursor > len(events):
            raise ValueError('checkpoint policy event cursor regressed')
        for event in events[start:cursor]:
            kind = event.get('type')
            if kind == 'tool':
                request = (event.get('structured') or {}).get('checkpoint_request')
                if event.get('name') == 'request_checkpoint':
                    if event.get('ok') is True and type(request) is dict and request.get('schema_version') == 1:
                        reason = request.get('reason')
                        if type(reason) is str and reason.strip():
                            self.pending = reason[:512]
                    continue
                self.progress = True
                delta = event.get('state_delta') or {}
                self.dirty |= bool(event.get('material_write') or event.get('changed_paths')
                    or any(delta.get(key) for key in ('created', 'modified', 'deleted', 'processes_started', 'processes_stopped')))
            elif kind in {'artifact_updated', 'environment_mutation', 'environment_mutated'}:
                self.dirty = self.progress = True
            elif kind in {'assistant_message', 'regular_inputs_consumed', 'deferred_inputs_consumed'}:
                self.progress = True
        self.observed_cursor = cursor

    def decide(self, kind):
        if kind in {'initial', 'final_response'}:
            return SaveDecision(True, kind)
        if kind not in {'after_tools', 'before_model'}:
            return SaveDecision(False, 'not_a_checkpoint_boundary')
        if self.pending is None:
            if kind == 'after_tools' and (self.progress or self.dirty):
                return SaveDecision(True, 'step')
            return SaveDecision(False, 'model_request_required')
        if not self.progress and not self.dirty:
            self.pending = None
            return SaveDecision(False, 'no_new_work')
        return SaveDecision(True, 'agent_request: ' + self.pending)

    def saved(self):
        self.last_saved = self.clock()
        self.pending = None
        self.dirty = self.progress = False
