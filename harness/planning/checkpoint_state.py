"""Full checkpoint data import, separate from partial-plan replay and validation."""
from pathlib import Path
import json
import re

from harness.core.portable_state import copy_plain, natural, require_fields, strings


def restore_evidence(reviews, value):
    value = copy_plain(value)
    require_fields(value, {'schema_version', 'root', 'receipts', 'collected', 'presented'})
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['root']) is not str or not Path(value['root']).is_absolute()
            or type(value['receipts']) is not dict):
        raise ValueError('invalid evidence checkpoint')
    strings(value['collected']); strings(value['presented'])
    if (len(set(value['collected'])) != len(value['collected'])
            or len(set(value['presented'])) != len(value['presented'])
            or not set(value['presented']) <= set(value['receipts'])):
        raise ValueError('invalid evidence delivery state')
    if len(value['receipts'])>65536:
        raise ValueError('evidence checkpoint exceeds bound')
    expected = {'receipt_id', 'task_id', 'contract_hash', 'file_hashes', 'metric_results', 'previews', 'preview_note'}
    for identifier, receipt in value['receipts'].items():
        if not re.fullmatch(r'[0-9a-f]{32}', identifier):
            raise ValueError('invalid evidence receipt ID')
        require_fields(receipt, expected)
        if (receipt['receipt_id'] != identifier or type(receipt['task_id']) is not str
                or type(receipt['contract_hash']) is not str or not re.fullmatch(r'[0-9a-f]{64}', receipt['contract_hash'])
                or type(receipt['file_hashes']) is not dict or type(receipt['metric_results']) is not list
                or type(receipt['previews']) is not dict or type(receipt['preview_note']) is not str):
            raise ValueError('invalid evidence receipt')
        for ref, digest in receipt['file_hashes'].items():
            path = Path(ref)
            if (not (reviews.executor.workspace/path).resolve().is_relative_to(reviews.executor.workspace.resolve())
                    or type(digest) is not str or not re.fullmatch(r'[0-9a-f]{64}', digest)):
                raise ValueError('invalid evidence file identity')
        if (any(type(v) is not str for v in receipt['previews'].values())
                or any(type(v) is not dict or type(v.get('passed')) is not bool for v in receipt['metric_results'])):
            raise ValueError('invalid evidence preview or metric history')
        # This validates archival identity, not present-day task correctness.
        # Existing validate() retains all current-file and assessment checks.
        path = reviews.root/(identifier+'.json')
        if reviews.executor.size(path)>128*1024*1024:
            raise ValueError('receipt archive exceeds import bound')
        if json.loads(reviews.executor.read_file(path).content) != receipt:
            raise ValueError('restored receipt archive differs from in-memory checkpoint')
    if reviews._receipts or reviews._collected or reviews._presented:
        raise RuntimeError('evidence import requires a fresh component')
    reviews._receipts = value['receipts']
    reviews._collected, reviews._presented = set(value['collected']), set(value['presented'])


def restore_plan(store, value):
    from .evidence import EvidenceReviews
    value = copy_plain(value)
    require_fields(value, {'schema_version', 'root', 'objective', 'serial', 'value', 'evidence'})
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['root']) is not str or not Path(value['root']).is_absolute()
            or value['objective'] != store.objective):
        raise ValueError('plan checkpoint configuration differs')
    natural(value['serial'])
    plan = value['value']
    require_fields(plan, {'version', 'objective', 'success_conditions', 'reason', 'tasks'})
    natural(plan['version'])
    strings(plan['success_conditions'])
    if (plan['objective'] != store.objective or type(plan['reason']) is not str
            or type(plan['tasks']) is not list or any(type(v) is not dict for v in plan['tasks'])
            or value['serial'] < plan['version'] or store._serial > value['serial']):
        raise ValueError('invalid plan checkpoint or future archive versions')
    store._validate(plan)
    evidence = EvidenceReviews(store.executor, store.root/'evidence')
    restore_evidence(evidence, value['evidence'])
    with store._lock:
        if store._value['version'] or store._value['tasks']:
            raise RuntimeError('plan import requires a fresh PlanStore')
        store._value, store._serial, store.evidence = plan, value['serial'], evidence
        store.checkpoint_origin_root = Path(value['root'])
