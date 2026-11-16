"""Validated context data import, separate from request and compaction policy."""
from dataclasses import asdict, is_dataclass
import json
import re

from harness.core.portable_state import copy_plain, natural, require_fields, strings
from .builder import ContextBuilder
from .ledger import TaskLedger
from .policy import ContextPolicy
from .prefix import PrefixObserver
from .store import ContextStore, ObservationStore


def restore_context(manager, value, *, observations):
    value = copy_plain(value)
    require_fields(value, manager.checkpoint_state())
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported context state')
    if type(observations) is not ObservationStore:
        raise ValueError('an explicitly assembled ObservationStore is required')
    policy = asdict(manager.policy) if is_dataclass(manager.policy) else manager.policy
    if (value['policy'] != json.loads(json.dumps(policy)) or value['workspace'] != str(manager.workspace)
            or type(value['max_context_tokens']) is not type(manager.max_context_tokens)
            or value['max_context_tokens'] != manager.max_context_tokens
            or type(value['summarizer_enabled']) is not bool
            or value['summarizer_enabled'] != (manager.summarizer is not None)):
        raise ValueError('context deployment configuration differs')
    expected_summary = (type(manager.summarizer).__module__ + ':' + type(manager.summarizer).__qualname__
                        if manager.summarizer is not None else None)
    if value['summarizer_implementation'] != expected_summary:
        raise ValueError('summarizer implementation differs')
    for key in ('compaction_count', 'prefix_count', 'last_request_size', 'summary_failures', 'estimation_error'):
        natural(value[key])
    for key in ('request_growth', 'output_sizes'):
        if type(value[key]) is not list or len(value[key]) > 32:
            raise ValueError('context estimator window exceeds bound')
        for number in value[key]:
            natural(number)
    for key in ('observed_event_ids', 'ingested_event_ids', 'microcompacted_contents'):
        strings(value[key])
        if len(set(value[key])) != len(value[key]):
            raise ValueError('duplicated context ingestion identity')
    for key in ('last_model_view', 'last_metrics', 'last_prefix_metrics'):
        if type(value[key]) is not dict:
            raise ValueError('invalid context view or metrics')
    for key in ('ledger_task', 'prefix_hash'):
        if type(value[key]) is not str:
            raise ValueError('invalid context text')
    if value['prefix_count'] and not re.fullmatch(r'[0-9a-f]{64}', value['prefix_hash']):
        raise ValueError('invalid context prefix digest')
    if value['summary_failure'] is not None and type(value['summary_failure']) is not str:
        raise ValueError('invalid summary failure')
    if (type(value['last_raw_messages']) is not list or
            any(type(v) is not dict for v in value['last_raw_messages']) or
            value['checkpoint'] is not None and type(value['checkpoint']) is not dict):
        raise ValueError('invalid context conversation data')
    if value['prefix_count'] > len(value['last_raw_messages']):
        raise ValueError('context prefix exceeds raw conversation')
    require_fields(value['builder_policy'], asdict(manager.builder.policy))
    for key, item in value['builder_policy'].items():
        if key == 'fallback_order':
            strings(item)
        elif key == 'max_recent_actions' and item is None:
            pass
        else:
            natural(item)
    builder_policy = ContextPolicy(**{**value['builder_policy'],
                                     'fallback_order': tuple(value['builder_policy']['fallback_order'])})
    # ContextBuilder parameters are data, but must match the selected policy.
    if builder_policy != manager.builder.policy:
        raise ValueError('context builder policy differs')
    store_data = value['store']
    require_fields(store_data, ContextStore().snapshot())
    for key in ('task_contract', 'goal'):
        if type(store_data[key]) is not str:
            raise ValueError('invalid context store task')
    for key in ('plan', 'facts', 'artifacts', 'failures', 'memory', 'active_skills', 'observation_refs'):
        strings(store_data[key])
    for key in ('planning', 'environment', 'verification'):
        if type(store_data[key]) is not dict:
            raise ValueError('invalid context store record')
    if type(store_data['actions']) is not list or any(type(v) is not dict for v in store_data['actions']):
        raise ValueError('invalid context store actions')
    store = ContextStore(**{**store_data, 'artifacts': set(store_data['artifacts'])})
    prefix = PrefixObserver()
    prefix.restore_checkpoint_state(value['prefix_observer'])
    # All validation completes before changing the live component. Paths from the
    # archive are descriptive; they never select a host directory or load code.
    assignments = {key: value[key] for key in ('last_model_view', 'last_metrics', 'compaction_count', 'last_prefix_metrics')}
    assignments.update({'_'+key: value[key] for key in (
        'checkpoint', 'prefix_count', 'prefix_hash', 'last_raw_messages', 'request_growth',
        'last_request_size', 'summary_failure', 'summary_failures', 'output_sizes', 'estimation_error')})
    assignments.update({'_'+key: set(value[key]) for key in (
        'observed_event_ids', 'ingested_event_ids', 'microcompacted_contents')})
    assignments.update(store=store, ledger=TaskLedger(value['ledger_task']),
                       builder=ContextBuilder(builder_policy), _prefix_observer=prefix,
                       observations=observations, archive_directory=observations.root)
    manager.__dict__.update(assignments)
