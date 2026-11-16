"""Conservative task-batch identity and completion evidence.

Only controller reports produced with the same task, source and run configuration
are eligible for skipping. Historical reports without these identities are ignored.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path


def tree_digest(root: Path, paths: tuple[Path, ...]) -> str:
    digest = hashlib.sha256()
    for base in paths:
        if not base.exists():
            continue
        files = [base] if base.is_file() else sorted(p for p in base.rglob('*') if p.is_file())
        for path in files:
            if any(part in {'__pycache__', '.git', '.harness'} for part in path.parts) or path.suffix in {'.pyc', '.pyo'}:
                continue
            if path.is_symlink():
                raise ValueError(f'symlinks are not supported in identity: {path}')
            name = path.relative_to(root).as_posix().encode()
            data = path.read_bytes()
            digest.update(len(name).to_bytes(8, 'big') + name)
            digest.update(len(data).to_bytes(8, 'big') + data)
    return digest.hexdigest()


def identity(root: Path, task: Path, model: str, agent: str, kwargs: list[str], platform: str | None, env_file: Path | None = None) -> dict:
    source = tuple(root / name for name in ('harness', 'safety', 'main.py', 'harbor_agent.py', 'harbor_capture.py'))
    relevant = {k: v for k, v in os.environ.items() if k.startswith(('MODEL_', 'HARNESS_', 'DEEPSEEK_'))}
    config = json.dumps(relevant, sort_keys=True).encode() + (env_file.read_bytes() if env_file else b'')
    return {'task_name': task.name, 'config_sha256': hashlib.sha256(config).hexdigest(), 'task_sha256': tree_digest(task, (task,)), 'source_sha256': tree_digest(root, source),
            'model': model, 'agent': agent, 'agent_kwargs': kwargs, 'platform': platform}


def run_verified(run: dict) -> bool:
    """A finished verifier is required; a zero reward remains a valid trial."""
    if run.get('returncode') != 0 or run.get('billing_paused') or run.get('error'):
        return False
    job = Path(run.get('job_dir', ''))
    for name in run.get('results', []):
        path = (job / name).resolve()
        if not path.is_relative_to(job.resolve()):
            continue
        try:
            result = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if (result.get('finished_at') and result.get('verifier_result') is not None
                and result.get('exception_info') is None):
            return True
    return False


def completed(report: dict, expected: dict) -> bool:
    actual = report.get('identity')
    if actual != expected:
        # Older reports predate task_name; match their recorded task path too.
        old_expected = {k: v for k, v in expected.items() if k != 'task_name'}
        if actual != old_expected or not expected.get('task_name') or Path(report.get('task', '')).name != expected['task_name']:
            return False
    if report.get('status') != 'completed':
        return False
    runs = report.get('runs', [])
    return (len(runs) in (1, 2) and [r.get('index') for r in runs] == list(range(1, len(runs) + 1))
            and all(run_verified(run) for run in runs)
            and report.get('cleanup', {}).get('status') == 'removed')
