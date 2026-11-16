"""S1a: cooperative boundary export into a host-mounted control directory.

The client runs inside the Harness container. It freezes the Runtime state and
writes a bounded, typed snapshot. It does not execute a model or a verifier, and
it does not claim that the environment was independently paused.
"""
from __future__ import annotations
import asyncio
import os
import math
import time
from pathlib import Path
from typing import Callable
from uuid import uuid4
from checkpoint_plugin.contracts import Boundary, ContractError, content_id
from checkpoint_plugin.contracts.agent import FrozenAgentData
from .watch import read_ack
REQUEST = 'request.json'
COMPONENTS = 'components'
ARCHIVES = 'archives'
MAX_COMPONENT = 64 * 1024 * 1024
MAX_TOTAL = 256 * 1024 * 1024
MAX_ARCHIVE = 256 * 1024 * 1024
MAX_ARCHIVES_TOTAL = 512 * 1024 * 1024
SELECTED = frozenset({'initial', 'before_model', 'after_tools', 'final_response'})
REQUIRED_COMPONENTS = frozenset({'conversation', 'model_view', 'context', 'task', 'plan', 'tools', 'budget', 'usage', 'pending_inputs', 'completed_children', 'recovery', 'api_audit', 'raw_events', 'configuration'})

def _control_io(root):
    from harness.executor import LocalExecutor
    from harness.permissions import PermissionPolicy
    from .control_io import ControlIO
    value = Path(root).resolve()
    anchor = value.parent
    while not anchor.exists() and anchor != anchor.parent:
        anchor = anchor.parent
    return ControlIO(LocalExecutor(anchor), PermissionPolicy())

def _safe_root(root: Path) -> Path:
    io = _control_io(root)
    value = Path(root).resolve()
    io.directory(value)
    return value


def write_snapshot(root: Path, frame: FrozenAgentData, *, wait_for_ack=False, worker_pid=None, source_io=None, trigger: str | None=None, pack_root: Path | None=None) -> str:
    if type(frame) is not FrozenAgentData:
        raise ContractError('typed FrozenAgentData required')
    base = _safe_root(root)
    request_id = uuid4().hex
    staging = base / ('.pending-' + request_id)
    io = _control_io(base)
    io.directory(staging)
    if source_io is None:
        source_io = io
    total = 0
    components = {}
    try:
        component_dir = staging / COMPONENTS
        io.directory(component_dir)
        for name, data in frame.components:
            if type(name) is not str or not name or len(name) > 128:
                raise ContractError('invalid component name')
            total += len(data)
            if total > MAX_TOTAL:
                raise ContractError('boundary state exceeds total bound')
            io.write_bytes(component_dir / name, data, maximum=MAX_COMPONENT)
            components[name] = {'sha256': content_id(data), 'size': len(data)}
        archive_dir = staging / ARCHIVES
        io.directory(archive_dir)
        archives = {}
        archive_total = 0
        packed_labels: dict[str, dict] = {}
        pack_store = None
        if pack_root is not None:
            from .archive_pack import PackStore
            pack_store = PackStore(Path(pack_root))
        if len(frame.archives) > 4096:
            raise ContractError('too many exchange archives')
        for label, source in frame.archives:
            if type(label) is not str or not label or len(label) > 120:
                raise ContractError('invalid archive label')
            if label in archives:
                raise ContractError('duplicate archive label')
            if pack_store is not None and source.is_dir():
                entries = pack_store.add_archive(Path(source))
                packed_labels[label] = entries
                archives[label] = {'path': 'archives.json', 'kind': 'packed', 'sha256': '', 'size': entries['size']}
                archive_total += entries['size']
                continue
            target = archive_dir / (label + ('.tar' if source.is_dir() else '.bin'))
            from .exchange_io import pack
            archives[label] = {'path': target.name, **pack(source, target, source_io, io, min(MAX_ARCHIVE, MAX_ARCHIVES_TOTAL - archive_total))}
            archive_total += archives[label]['size']
        if packed_labels:
            from .archive_pack import write_manifest
            payload = write_manifest(archive_dir / 'archives.json', packed_labels)
            pack_store.flush_index()
            for label in packed_labels:
                archives[label]['sha256'] = payload['sha256']
        request = {'schema_version': 1, 'kind': 'science-boundary-request-v1', 'request_id': request_id, 'wait_for_ack': wait_for_ack, 'worker_pid': worker_pid, 'boundary': frame.boundary.dictionary(), 'adapter': frame.adapter, 'adapter_version': frame.adapter_version, 'components': components, 'archives': archives, 'trigger': trigger}
        io.write(staging / REQUEST, request)
        if wait_for_ack:
            io.write(staging / 'waiting.json', {'active': True})
        io.publish_directory(staging, base / request_id)
        return request_id
    except BaseException:
        io.remove_owned_tree(staging)
        raise


class BoundaryClient:
    """Minimal cooperative client; S1a writes, S1b adds host ack/import."""

    def __init__(self, freeze: Callable[..., FrozenAgentData], control_root: Path, *, selected=SELECTED, wait_seconds: float=0.0, require_joint=False, policy=None):
        if not callable(freeze):
            raise TypeError('freeze callable required')
        if type(selected) is not frozenset or not selected <= SELECTED:
            raise ValueError('invalid boundary selection')
        if type(wait_seconds) not in (int, float) or not math.isfinite(wait_seconds) or wait_seconds < 0:
            raise ValueError('negative wait is invalid')
        self.freeze, self.root, self.selected = (freeze, _safe_root(control_root), selected)
        if type(require_joint) is not bool or (require_joint and (not wait_seconds)):
            raise ValueError('joint checkpoints require a blocking acknowledgement')
        from .policy import CheckpointPolicy
        self.policy = policy if policy is not None else CheckpointPolicy()
        self.require_joint = require_joint
        self.wait_seconds = wait_seconds

    def _stop_managed_processes(self, session) -> None:
        """Best-effort quiescence at the final boundary: the turn already ended,
        so leftover managed processes are cleanup, not work in flight."""
        tools = getattr(session, 'tools', None)
        executor = getattr(getattr(tools, 'context', None), 'executor', None)
        if executor is None:
            return
        try:
            processes = executor.checkpoint_processes()
        except Exception:
            return
        for process in processes:
            if getattr(process, 'running', False):
                try:
                    executor.stop_process(process.handle)
                except Exception:
                    pass

    async def at_boundary(self, session, boundary, context, control):
        if boundary.kind not in self.selected:
            return
        control.check_active()
        events = getattr(session, 'events', [])
        cursor = getattr(boundary, 'event_cursor', len(events))
        inherited = getattr(getattr(session, 'recorder', None), 'offset', 0)
        self.policy.observe(events, cursor=cursor, inherited_cursor=inherited)
        decision = self.policy.decide(boundary.kind)
        emit = getattr(session, 'emit', lambda *args, **kwargs: None)
        if not decision.save:
            if decision.reason != 'no_key_milestone':
                emit('science_checkpoint_deferred', boundary=boundary.kind, reason=decision.reason)
            return
        if boundary.kind == 'final_response':
            self._stop_managed_processes(session)
        try:
            frame = self.freeze(session, boundary, context, control)
        except Exception as failure:
            from checkpoint_plugin.errors import CapabilityError
            if not isinstance(failure, CapabilityError):
                raise
            if boundary.kind == 'initial':
                raise
            emit('science_checkpoint_deferred', boundary=boundary.kind, reason='state_not_quiescent', error_type=type(failure).__name__, error=str(failure)[:500])
            return
        from .control_io import ControlIO
        tools = getattr(session, 'tools', None)
        source_io = ControlIO(tools.context.executor, tools.permission_policy, session.emit) if tools is not None else None
        request_id = write_snapshot(self.root, frame, source_io=source_io, wait_for_ack=bool(self.wait_seconds), worker_pid=os.getpid() if self.wait_seconds else None, trigger=decision.reason, pack_root=self.root / '.archive-pack')
        emit = getattr(session, 'emit', lambda *args, **kwargs: None)
        emit('science_checkpoint_requested', request_id=request_id, boundary=frame.boundary.dictionary(), waiting=bool(self.wait_seconds), trigger=decision.reason)
        if self.wait_seconds:
            try:
                deadline = time.monotonic() + self.wait_seconds
                while True:
                    control.check_active()
                    try:
                        ack = read_ack(self.root, request_id)
                        if not ack.get('root_index'):
                            raise ContractError('acknowledgement has no durable checkpoint root')
                        if self.require_joint and ack.get('coverage') != 'joint':
                            raise ContractError('state-only acknowledgement cannot resume a joint checkpoint client')
                        self.policy.saved()
                        emit('science_checkpoint_acknowledged', request_id=request_id, root=ack['root_index'])
                        return request_id
                    except FileNotFoundError:
                        if time.monotonic() >= deadline:
                            raise ContractError('boundary acknowledgement timed out')
                        await asyncio.sleep(0.1)
                    except ContractError as failure:
                        if boundary.kind == 'initial':
                            raise
                        emit('science_checkpoint_deferred', boundary=boundary.kind, reason='capture_failed', error_type=type(failure).__name__, error=str(failure)[:500])
                        return request_id
            finally:
                await asyncio.shield(self._withdraw(request_id))
        self.policy.saved()
        return request_id

    async def _withdraw(self, request_id):
        from harness.executor import LocalExecutor
        from harness.permissions import PermissionPolicy
        from .control_io import ControlIO
        executor = LocalExecutor(self.root)
        io = ControlIO(executor, PermissionPolicy())
        while True:
            io.io.check(self.root / '.harness/workspace.lock', True)
            try:
                lease = executor.acquire_workspace_lease()
                break
            except RuntimeError:
                await asyncio.sleep(0.1)
        try:
            io.write(self.root / request_id / 'waiting.json', {'active': False})
        finally:
            lease.close()
