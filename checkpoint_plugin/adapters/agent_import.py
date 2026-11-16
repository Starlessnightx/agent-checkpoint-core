"""Explicit Harness data import into a fresh assembly, independent of Docker.

Completed processes are restored as read-only records with no PID authority.
Completed children retain results and delivery state, without restarted workers.
Optional planning uses an explicit deployment assembler. Environment restoration
is not asserted by this adapter.
"""
from dataclasses import dataclass, fields, replace
from pathlib import Path
import math

from checkpoint_plugin.contracts import ContractError, canonical, decode
from checkpoint_plugin.contracts import content_id
from checkpoint_plugin.errors import CapabilityError
from harness.context.builder import ModelContext
from harness.core.boundaries import RestoredTurnPoint
from harness.core.events import EventRecorder
from harness.core.interactions import ToolCatalogScope
from harness.core.portable_state import natural, require_fields, strings
from harness.core.state import RuntimeState, TaskUsageLedger, UsageState
from harness.core.task import TaskContract
from harness.core.turn import TurnLoopDriver
from harness.environment.state import EnvironmentState
from harness.executor.base import ProcessStatus
from harness.protocol.operations import operation_from_dict, Steer
from harness.storage import ExperimentIO
from harness.tasks import RegularTask
from .agent_state import plain, _complete_groups


@dataclass(frozen=True)
class ImportedHarnessSession:
    session: object
    driver: TurnLoopDriver
    runner: RegularTask
    source_root: str
    historical_usage: dict

    def usage_report(self):
        baseline = self.historical_usage['task'] or self.historical_usage['session']
        total = self.session.task_usage.snapshot() if self.session.task_usage else self.session.usage.dictionary()
        added = {key: total[key]-value for key, value in baseline.items()}
        if any(value < 0 for value in added.values()):
            raise ContractError('historical usage regressed after import')
        return {'historical': dict(baseline), 'added': added, 'total': total}


def _usage(value, *, cost=False):
    expected = set(UsageState.__dataclass_fields__) | ({'cost_usd'} if cost else set())
    if type(value) is not dict or set(value) != expected:
        raise ContractError('invalid historical usage fields')
    for key, number in value.items():
        if key == 'cost_usd':
            if type(number) not in (int, float) or not math.isfinite(number) or number < 0:
                raise ContractError('invalid historical task cost')
        elif type(number) is not int or number < 0:
            raise ContractError('invalid historical token usage')
    return UsageState(**{k: v for k, v in value.items() if k != 'cost_usd'})


@dataclass(frozen=True)
class CatalogRebinding:
    specifications: dict
    provenance: dict


class HarnessStateImporter:
    def __init__(self, *, model_validator, planning_assembler=None,catalog_rebinder=None):
        """Providers are deployment-owned callables, never loaded from archives.

        model_validator(saved_provider, target_model, target_session_id) raises if
        continuation/configuration is incompatible; it must obtain fresh authority.
        """
        if not callable(model_validator):
            raise TypeError('explicit model compatibility provider is required')
        self.model_validator = model_validator
        if planning_assembler is not None and not callable(planning_assembler):
            raise TypeError('planning assembly must be a deployment-owned callable')
        self.planning_assembler = planning_assembler
        if catalog_rebinder is not None and not callable(catalog_rebinder):
            raise TypeError('catalog rebinder must be deployment owned')
        self.catalog_rebinder=catalog_rebinder

    def restore_snapshot(self, snapshot_root, runtime, *, extract_root=None):
        """Import agent state straight from an independent snapshot directory.

        The snapshot already holds canonical component files and archive payloads
        with recorded hashes, so no host-side CAS materialisation is required.
        """
        from types import SimpleNamespace
        import hashlib
        import json as _json
        from checkpoint_plugin.contracts import Boundary
        root = Path(snapshot_root)
        try:
            request = _json.loads((root / 'request.json').read_text())
        except OSError as exc:
            raise ContractError('snapshot request is unavailable') from exc
        if (type(request) is not dict or request.get('schema_version') != 1
                or request.get('kind') != 'science-boundary-request-v1'):
            raise ContractError('invalid snapshot request')
        data = {}
        for name, record in request['components'].items():
            try:
                raw = (root / ('component_' + name)).read_bytes()
            except OSError as exc:
                raise ContractError('snapshot component is missing: ' + name) from exc
            if (len(raw) != record['size']
                    or hashlib.sha256(raw).hexdigest() != record['sha256']):
                raise ContractError('snapshot component differs: ' + name)
            data[name] = decode(raw)
        import tarfile
        archives = {}
        extract_root = Path(extract_root) if extract_root is not None else root.parent / ('.import-' + root.name)
        for label, record in request['archives'].items():
            if record['kind'] == 'missing':
                continue
            if record['kind'] == 'packed':
                import json as _json
                from science_checkpoint.archive_pack import PackStore, restore_manifest
                manifest_path = root / record['path']
                payload = _json.loads(manifest_path.read_text())
                canonical = _json.dumps(payload['labels'], sort_keys=True,
                                        separators=(',', ':')).encode()
                if hashlib.sha256(canonical).hexdigest() != record['sha256']:
                    raise ContractError('archive manifest differs: ' + label)
                store = PackStore(root.parent / '.archive-pack')
                restore_manifest(payload, store, extract_root)
                for name in payload['labels']:
                    archives[name] = {'kind': 'tree', 'path': extract_root / name,
                                      'sha256': record['sha256'], 'size': record['size']}
                continue
            path = root / ('archive_' + label)
            try:
                size = path.stat().st_size
            except OSError as exc:
                raise ContractError('snapshot archive is missing: ' + label) from exc
            digest = hashlib.sha256()
            with path.open('rb') as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b''):
                    digest.update(block)
            if size != record['size'] or digest.hexdigest() != record['sha256']:
                raise ContractError('snapshot archive differs: ' + label)
            if record['kind'] == 'directory':
                target = extract_root / label
                target.mkdir(parents=True, exist_ok=True)
                with tarfile.open(path, 'r:') as archive:
                    archive.extractall(target, filter='data')
                archives[label] = {'kind': 'tree', 'path': target,
                                   'sha256': record['sha256'], 'size': record['size']}
                continue
            if record['kind'] != 'file':
                raise CapabilityError('unsupported snapshot archive kind: ' + record['kind'])
            archives[label] = {'kind': 'file', 'path': path,
                               'sha256': record['sha256'], 'size': record['size']}
        boundary = request['boundary']
        saved = SimpleNamespace(
            components=data, archives=archives,
            boundary=Boundary(boundary['kind'], boundary['event_cursor'], boundary['sampling_index']),
            envelope=SimpleNamespace(adapter=request['adapter'],
                                     adapter_version=request['adapter_version']))
        return self._restore(saved, runtime, archive_paths=archives, source=str(root))

    def _restore(self, saved, runtime, *, archive_paths, source=None):
        if (saved.envelope.adapter, saved.envelope.adapter_version) != ('harness-turn', '1'):
            raise CapabilityError('unsupported Harness Agent state version; explicit migration is required')
        data, spec = saved.components, runtime.spec
        task = data['task']
        _complete_groups(data['conversation'])
        require_fields(task['runtime_state'], RuntimeState.__dataclass_fields__)
        for key in ('turn', 'rejected_completions'):
            natural(task['runtime_state'][key])
        for key in ('phase', 'status', 'final_text'):
            if type(task['runtime_state'][key]) is not str:
                raise ContractError('invalid Runtime text state')
        if (type(task['runtime_state']['metadata']) is not dict or
                task['runtime_state']['termination'] is not None and type(task['runtime_state']['termination']) is not str):
            raise ContractError('invalid Runtime metadata')
        strings(task['artifacts'])
        if task['parent_session_id'] is not None or task['agent_role'] != 'parent':
            raise CapabilityError('active child-session scheduling needs its own restore provider')
        if data['configuration']['workspace'] != str(spec.workspace):
            raise CapabilityError('strict continuation requires the same in-environment workspace path')
        contract_data = task['contract']
        if set(contract_data) != {f.name for f in fields(TaskContract)}:
            raise ContractError('task contract fields differ')
        contract = TaskContract(**{k: tuple(v) if k in {
            'mentioned_absolute_paths', 'required_artifacts', 'completion_conditions', 'success_conditions'} else v
                                  for k, v in contract_data.items()})
        configured = {'configured': plain(spec.budget), 'token_budget': data['budget']['token_budget'],
                      'tool_call_budget': spec.budget.max_tool_calls, 'orchestration': plain(spec.orchestration),
                      'recovery_policy': plain(spec.recovery)}
        if data['budget']['token_budget'] is not None and natural(data['budget']['token_budget']) == 0:
            raise ContractError('invalid historical agent token budget')
        if (canonical(configured) != canonical(data['budget']) or
                canonical(plain(spec.context)) != canonical(data['configuration']['context']) or
                canonical(plain(spec.model)) != canonical(data['configuration']['model'])):
            raise CapabilityError('candidate configuration requires an explicit recorded intervention/migration')
        session_usage = _usage(data['usage']['session'])
        task_usage = _usage(data['usage']['task'], cost=True) if data['usage']['task'] is not None else None
        point = RestoredTurnPoint(saved.boundary.kind, task['turn_context']['steps'],
                                  task['sampling_index'], task['turn_context']['unsupported_output_notices'])
        if task['boundary']['sampling_step'] != point.steps:
            raise ContractError('Runtime and checkpoint sampling-step cursors differ')
        session = runtime.create_session(contract)
        try:
            compatible = self.model_validator(data['configuration']['provider'], session.model, session.session_id)
            if compatible is not None and compatible is not True:
                raise CapabilityError('model compatibility provider rejected continuation')
            if (canonical(plain(session.orchestrator.checkpoint_state())) != canonical(task['policies']['orchestrator'])
                    or canonical(plain(session.environment_repair.checkpoint_state())) != canonical(task['policies']['environment_repair'])):
                raise CapabilityError('assembled policy or child catalog differs')
            children = data['completed_children']
            current_catalog=plain(session.subagents.checkpoint_state()['specs'])
            catalog_rebinding=None
            if canonical(current_catalog)!=canonical(children['specs']):
                if self.catalog_rebinder is None:
                    raise CapabilityError('assembled policy or child catalog differs')
                catalog_rebinding=self.catalog_rebinder(decode(canonical(children['specs'])),
                    decode(canonical(current_catalog)))
                if (type(catalog_rebinding) is not CatalogRebinding
                        or canonical(catalog_rebinding.specifications)!=canonical(current_catalog)
                        or type(catalog_rebinding.provenance) is not dict or not catalog_rebinding.provenance):
                    raise CapabilityError('catalog rebinder did not prove current specifications')
                children={**children,'specs':decode(canonical(current_catalog))}
            if children['max_parallel'] != session.subagents.max_parallel:
                raise CapabilityError('child parallelism differs')
            tools = data['tools']
            for name, manager in (('mcp', session.mcp_manager), ('lsp', session.lsp_manager)):
                current = manager.checkpoint_state() if manager is not None else None
                if current != tools[name]:
                    raise CapabilityError(name+' needs a capable state importer')
            if session.tools.context.background_tasks is None or tools['background'] is None:
                raise CapabilityError('background scheduler assembly differs')
            paths = {label: record['path'] for label, record in archive_paths.items()}
            if set(paths) != set(saved.archives):
                raise ContractError('snapshot archive coverage differs')
            if data['plan'] is not None:
                plan_root = saved.archives.get('plans')
                if 'plans' not in paths or plan_root['kind'] != 'tree':
                    raise ContractError('plan archive is missing')
                if self.planning_assembler is None:
                    raise CapabilityError('optional planning requires an explicit matching deployment assembler')
                self.planning_assembler(data['plan'], session, paths['plans'])
            elif session.tools.context.plan_store is not None:
                raise CapabilityError('candidate unexpectedly enabled optional planning')
            if session.hooks is None or tools['hooks'] is None:
                raise CapabilityError('middleware assembly differs')
            session.hooks.restore_checkpoint_state(tools['hooks'])
            processes = []
            for record in tools['executor_processes']:
                require_fields(record, ProcessStatus.__dataclass_fields__)
                if type(record['handle']) is not str:
                    raise ContractError('invalid completed process handle')
                label = 'process_'+content_id(record['handle'].encode())[:16]
                if label not in paths or saved.archives[label]['kind'] != 'file':
                    raise ContractError('completed process log archive is missing')
                log_path = paths[label]
                if archive_paths is not None:
                    # Completed process logs must live inside the workspace.
                    target = Path(runtime.spec.workspace) / '.harness' / 'imported-processes' / label
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(log_path.read_bytes())
                    log_path = target
                processes.append(ProcessStatus(**{**record, 'log_path': log_path}))
            if processes:
                inherit = getattr(session.tools.context.executor, 'inherit_completed_processes', None)
                if not callable(inherit):
                    raise CapabilityError('Executor has no completed process data importer')
                inherit(tuple(processes))
            child_paths = {}
            for child_id, outcome in children['results'].items():
                if type(outcome) is not dict:
                    raise ContractError('invalid child outcome')
                if outcome.get('result', {}).get('trajectory_path'):
                    label = 'child_'+content_id(child_id.encode())[:16]
                    if label not in paths or saved.archives[label]['kind'] != 'tree':
                        raise ContractError('completed child audit archive is missing')
                    child_paths[child_id] = paths[label]
            session.subagents.restore_completed_checkpoint_state(children, archive_paths=child_paths)
            with ToolCatalogScope(session.tools):
                session.tools.restore_checkpoint_state(tools['registry'])
            session.tools.context.background_tasks.restore_idle_checkpoint_state(tools['background'])
            labels = sorted((k for k in paths if k.startswith('observations_')),
                            key=lambda k:int(k.removeprefix('observations_')))
            if not labels or labels != ['observations_'+str(i) for i in range(len(labels))]:
                raise ContractError('observation archive sequence differs')
            observations = session.context_manager.observations
            observations.inherit_checkpoint_archives(tuple(paths[k] for k in labels))
            session.api_audit_prefixes = tuple(paths[label] for label in sorted(paths)
                if (label == 'api_audit' or label.startswith('api_audit_prefix_'))
                and saved.archives[label]['kind'] in {'tree', 'file'})
            session.context_manager.restore_checkpoint_state(data['context'], observations=observations)
            session.recovery.restore_checkpoint_state(data['recovery'])
            session.messages = data['conversation']
            session.state = RuntimeState(**task['runtime_state'])
            session.state.status = 'idle'
            session.usage = session_usage
            session.task_usage = TaskUsageLedger(task_usage, data['usage']['task']['cost_usd']) if task_usage else None
            session.token_budget = data['budget']['token_budget']
            session.artifacts.paths = set(task['artifacts'])
            session.environment = EnvironmentState(**task['environment']) if task['environment'] is not None else None
            session.tools.context.environment_state = EnvironmentState(**tools['observed_environment']) if tools['observed_environment'] is not None else None
            current = data['model_view']['current']
            session.model_context = ModelContext(**current) if current is not None else None
            pending = data['pending_inputs']
            require_fields(pending, {'turn','incoming','deferred'})
            if any(type(values) is not list for values in pending.values()):
                raise ContractError('invalid saved input queues')
            inputs = [('deferred',value) for value in pending['deferred']]
            inputs += [('turn',value) for value in [*pending['turn'], *pending['incoming']]]
            seen = set()
            turn_inputs = []
            for destination, value in inputs:
                operation = operation_from_dict(value)
                if type(operation) is not Steer or operation.operation_id in seen:
                    raise ContractError('pending input is duplicated or has unsupported authority')
                if (operation.session_id != task['session_id'] or
                        destination=='turn' and operation.turn_id != task['boundary']['turn_id']):
                    raise ContractError('saved input source scope differs')
                seen.add(operation.operation_id)
                rebound = replace(operation, session_id=session.session_id)
                (session.deferred_inputs if destination=='deferred' else turn_inputs).append(rebound)
            io = ExperimentIO(session.tools.context.executor, session.tools.permission_policy, session.emit)
            path = io.check(session.recorder.path.parent/'branch-trajectory.jsonl', True)
            recorder = EventRecorder(path, executor=session.tools.context.executor)
            recorder.initialize()
            recorder.inherit_prefix(data['raw_events']['events'])
            session.recorder = recorder
            session.emit('checkpoint_session_imported', source_root=source, source_session_id=task['session_id'],
                         inherited_events=saved.boundary.event_cursor, historical_usage=data['usage'],
                         authority_restored=False, recoverable_environment=False, publication='local_only')
            if catalog_rebinding is not None:
                session.emit('checkpoint_child_catalog_rebound',
                    provenance=decode(canonical(catalog_rebinding.provenance)),
                    source_catalog=content_id(canonical(data['completed_children']['specs'])),
                    target_catalog=content_id(canonical(current_catalog)),workers_restarted=0)
            if processes:
                session.emit('checkpoint_process_history_imported', handles=[p.handle for p in processes],
                             pid_authority_restored=False, running_processes=0)
            if children['results']:
                session.emit('checkpoint_child_history_imported', child_ids=list(children['results']),
                             workers_restarted=0, authority_restored=False)
            if data['plan'] is not None:
                session.emit('checkpoint_plan_data_imported', source_root=data['plan']['root'],
                             target_root=str(session.tools.context.plan_store.root),
                             current_evidence_verified=False, user_approvals_restored=False)
            driver = TurnLoopDriver(spec, session.model, runtime.execute_tool_calls)
            driver.restore_turn(session, point,
                                prepare=lambda target: target.tools.restore_checkpoint_state(tools['registry']))
            return ImportedHarnessSession(session, driver, RegularTask(session, driver, restored=True,
                                                                       restored_inputs=tuple(turn_inputs)),
                                          source, data['usage'])
        except BaseException as failure:
            failures = [failure]
            try:
                session.emit('checkpoint_session_import_failed', source_root=source,
                             exception_type=type(failure).__name__, authority_restored=False)
            except BaseException as audit_failure:
                failures.append(audit_failure)
            try:
                session.close()
            except BaseException as cleanup:
                failures.append(cleanup)
            if len(failures)>1:
                raise BaseExceptionGroup('Agent import and cleanup failed', failures)
            raise
