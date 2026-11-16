"""Completed child data import; no worker execution or archived code loading."""
from concurrent.futures import Future
from dataclasses import asdict, fields, replace
from pathlib import Path
import json
import re

from harness.core.portable_state import copy_plain, natural, require_fields, strings
from .base import SubagentResult


class ArchivedChildFailure(RuntimeError):
    def __init__(self, source_type, message):
        super().__init__(message)
        self.source_type = source_type


def restore_children(coordinator, value, *, archive_paths):
    value = copy_plain(value)
    require_fields(value, {'schema_version', 'results', 'roles', 'cancelled', 'steers',
                          'notifications', 'notified', 'task_ids', 'max_parallel', 'specs'})
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(value['max_parallel']) is not int or value['max_parallel'] != coordinator.max_parallel
            or value['specs'] != json.loads(json.dumps({k: asdict(v) for k,v in coordinator.specs.items()}))):
        raise ValueError('child deployment configuration differs')
    for name in ('results', 'roles', 'steers', 'task_ids'):
        if type(value[name]) is not dict:
            raise ValueError('invalid completed child state')
    ids = set(value['results'])
    if (len(ids)>65536 or set(value['roles']) != ids or
            any(not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', key) for key in ids) or
            any(role not in coordinator.specs for role in value['roles'].values())):
        raise ValueError('invalid completed child identity')
    for key in ('cancelled', 'notified'):
        strings(value[key])
        if len(set(value[key])) != len(value[key]) or not set(value[key]) <= ids:
            raise ValueError('invalid child delivery state')
    if set(value['notified']) != ids:
        raise ValueError('child checkpoint lacks completion notification reconciliation')
    for identifier, items in value['steers'].items():
        if identifier not in ids:
            raise ValueError('steering references an unknown child')
        strings(items)
    if not set(value['task_ids']) <= ids or any(type(v) is not str for v in value['task_ids'].values()):
        raise ValueError('invalid child task association')
    if type(archive_paths) is not dict:
        raise ValueError('explicit child archive mapping is required')
    imported, remaps, needed = {}, {}, set()
    root = coordinator.workspace.resolve()
    for identifier, record in value['results'].items():
        future = Future()
        if type(record) is not dict:
            raise ValueError('invalid completed child record')
        if set(record) == {'result'}:
            data = record['result']
            require_fields(data, {f.name for f in fields(SubagentResult)})
            if (data['agent_id'] != identifier or data['role'] != value['roles'][identifier]
                    or data['status'] not in {'completed', 'failed', 'cancelled'}
                    or data['workspace_mode'] not in coordinator.specs[data['role']].workspace_modes):
                raise ValueError('completed child result identity differs')
            for key in ('steps', 'tool_calls', 'compactions'):
                natural(data[key])
            for key in ('summary', 'patch', 'stop_reason', 'trajectory_path', 'workspace_path'):
                if type(data[key]) is not str:
                    raise ValueError('invalid completed child text')
            if data['error'] is not None and type(data['error']) is not str:
                raise ValueError('invalid completed child error')
            for key in ('evidence_refs', 'artifacts'):
                strings(data[key])
            if type(data['events']) is not list or any(type(v) is not dict for v in data['events']):
                raise ValueError('invalid child audit events')
            if (type(data['verification']) is not list or
                    any(type(v) is not dict and v != 'not_evaluated' for v in data['verification'])):
                raise ValueError('invalid child evidence')
            if type(data['usage']) is not dict:
                raise ValueError('invalid child usage')
            for count in data['usage'].values():
                natural(count)
            result = SubagentResult(**{**data, **{key: tuple(data[key]) for key in
                                      ('evidence_refs', 'artifacts', 'verification', 'events')}})
            if result.trajectory_path:
                needed.add(identifier)
                destination = archive_paths.get(identifier)
                if (not isinstance(destination, Path) or not destination.is_absolute()
                        or not destination.resolve().is_relative_to(root)):
                    raise ValueError('completed child archive escapes target workspace')
                original = Path(result.trajectory_path)
                target = destination/original.name
                if not coordinator.parent_session.tools.context.executor.is_file(target):
                    raise ValueError('restored child trajectory is missing')
                old_parent = str(original.parent).rstrip('/')+'/'
                new_parent = destination.relative_to(root).as_posix().rstrip('/')+'/'
                def remap(ref, old=old_parent, new=new_parent):
                    return new+ref[len(old):] if ref.startswith(old) else ref
                remaps[identifier] = remap
                result = replace(result, trajectory_path=target.relative_to(root).as_posix(),
                                 evidence_refs=tuple(remap(v) for v in result.evidence_refs),
                                 artifacts=tuple(remap(v) for v in result.artifacts))
            future.set_result(result)
        elif set(record) == {'error_type', 'error'} and all(type(v) is str for v in record.values()):
            if record['error_type'] == 'CancelledError':
                future.cancel()
            else:
                future.set_exception(ArchivedChildFailure(record['error_type'], record['error']))
        else:
            raise ValueError('invalid completed child outcome')
        imported[identifier] = future
    if set(archive_paths) != needed:
        raise ValueError('completed child archive coverage differs')
    if type(value['notifications']) is not list:
        raise ValueError('invalid child notifications')
    for notice in value['notifications']:
        if (type(notice) is not dict or notice.get('agent_id') not in ids
                or notice.get('kind') != 'subagent'):
            raise ValueError('invalid pending child notification')
        remap = remaps.get(notice['agent_id'])
        if remap:
            if 'trajectory_path' in notice:
                notice['trajectory_path'] = remap(notice['trajectory_path'])
            if 'evidence_refs' in notice:
                strings(notice['evidence_refs'])
                notice['evidence_refs'] = [remap(v) for v in notice['evidence_refs']]
    with coordinator._lock:
        if coordinator._futures:
            raise RuntimeError('child history requires a fresh coordinator')
        coordinator._futures = imported
        coordinator._roles, coordinator._steers = value['roles'], value['steers']
        coordinator._cancelled, coordinator._notified = set(value['cancelled']), set(value['notified'])
        coordinator._notifications, coordinator._task_ids = value['notifications'], value['task_ids']
        coordinator._historical_ids = set(imported)
