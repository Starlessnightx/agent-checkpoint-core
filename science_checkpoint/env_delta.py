"""Environment checkpoints from the container writable layer.

Every write inside a container (``/usr``, ``/etc``, ``site-packages``,
``/app`` ...) lands in the same writable layer.  ``docker diff`` enumerates
those paths, so copying exactly those files captures *all* environment
mutations without bind-mounting system directories or requiring a privileged
container.

Each capture produces an ``envdiff`` directory:

    envdiff/manifest.json    per-file sha256/size plus the deletion list
    envdiff/<path>           changed or added files (hardlinked when unchanged)

Unchanged files are hardlinked to the previous checkpoint, so repeated
captures cost almost no disk space.  ``environment.json`` stores variables and
package fingerprints for restore-time verification.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import re
import subprocess
import tarfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

SECRET_PATTERN = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)", re.IGNORECASE)
DEFAULT_EXCLUDES = ("/.harness/", "/.checkpoints/", "/installed-harness/")
MANIFEST = "manifest.json"
ENVIRONMENT = "environment.json"
DELETIONS = "deletions.json"

Runner = Callable[..., "subprocess.CompletedProcess[bytes]"]


def default_runner(argv: Sequence[str], *, input: bytes | None = None, timeout: int = 600):
    return subprocess.run(list(argv), input=input, capture_output=True, timeout=timeout)


@dataclass(frozen=True)
class DiffEntry:
    kind: str  # "A" added, "C" changed, "D" deleted
    path: str  # absolute container path


def parse_diff(text: str) -> list[DiffEntry]:
    entries: list[DiffEntry] = []
    for line in text.splitlines():
        if len(line) < 3:
            continue
        kind, path = line[0], line[1:].strip()
        if kind not in {"A", "C", "D"} or not path.startswith("/"):
            continue
        entries.append(DiffEntry(kind, path))
    return entries


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _dedupe(payload: Path, previous_payload: Path | None, stats: dict | None = None) -> dict[str, dict]:
    """Replace unchanged files with hardlinks to the previous checkpoint."""
    record: dict[str, dict] = {}
    if not payload.exists():
        return record
    for path in sorted(payload.rglob("*")):
        relative = path.relative_to(payload)
        if path.is_symlink():
            record[str(relative)] = {"type": "symlink", "target": os.readlink(path),
                                     "size": 0, "linked": False}
            continue
        if not path.is_file():
            continue
        digest = _sha256_file(path)
        size = path.stat().st_size
        linked = False
        if previous_payload is not None:
            old = previous_payload / relative
            if old.is_file() and not old.is_symlink() and old.stat().st_size == size:
                if _sha256_file(old) == digest:
                    path.unlink()
                    os.link(old, path)
                    linked = True
        stat_line = (stats or {}).get("/" + str(relative))
        record[str(relative)] = {"sha256": digest, "size": size, "linked": linked,
                                 "mtime": stat_line[1] if stat_line else None}
    return record


def _tar_listing(paths: Sequence[str]) -> bytes:
    return ("\n".join(paths) + "\n").encode()


def _extract(payload: Path, tar_bytes: bytes) -> None:
    payload.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(tar_bytes), mode="r:") as archive:
        archive.extractall(payload, filter="tar")


def _excluded(path: str, patterns: Sequence[str]) -> bool:
    probe = path.rstrip("/") + "/"
    return any(pattern in probe for pattern in patterns)


def _stat_changes(container: str, paths: Sequence[str], run: Runner) -> dict[str, tuple[int, str]]:
    """Return {path: (size, mtime)} for the changed paths, in ARG_MAX-safe chunks."""
    result: dict[str, tuple[int, str]] = {}
    chunk = 500
    for start in range(0, len(paths), chunk):
        batch = paths[start:start + chunk]
        command = ["docker", "exec", container, "stat", "-c", "%n|%s|%y", "--", *batch]
        probe = run(command)
        for line in probe.stdout.decode(errors="replace").splitlines():
            name, _, rest = line.partition("|")
            size_text, _, mtime = rest.partition("|")
            if not name or not size_text.strip().isdigit():
                continue
            result[name] = (int(size_text), mtime.strip())
    return result


def capture(container: str, out_dir: str | Path, previous_dir: str | Path | None = None,
            run: Runner = default_runner, exclude: Sequence[str] = DEFAULT_EXCLUDES) -> dict:
    """Capture one environment delta for ``container`` into ``out_dir``."""
    out = Path(out_dir)
    payload = out / "envdiff"
    payload.mkdir(parents=True, exist_ok=True)
    diff = run(["docker", "diff", container])
    if diff.returncode != 0:
        raise RuntimeError(f"docker diff failed: {diff.stderr.decode(errors='replace').strip()}")
    entries = parse_diff(diff.stdout.decode(errors="replace"))
    files = [entry.path for entry in entries
             if entry.kind in {"A", "C"} and not _excluded(entry.path, exclude)]
    previous_manifest: dict[str, dict] = {}
    if previous_dir is not None:
        try:
            previous_manifest = json.loads((Path(previous_dir) / MANIFEST).read_text())["files"]
        except (OSError, ValueError, KeyError):
            previous_manifest = {}
    stats = _stat_changes(container, files, run) if files else {}
    selected = []
    for path in files:
        relative = path.lstrip("/")
        recorded = previous_manifest.get(relative)
        current = stats.get(path)
        if recorded and current and recorded.get("mtime") == current[1] and recorded.get("size") == current[0]:
            continue
        selected.append(path)
    files = selected
    deletions = sorted({entry.path for entry in entries
                        if entry.kind == "D" and not _excluded(entry.path, exclude)})
    (out / DELETIONS).write_text(json.dumps(deletions, indent=1) + "\n")

    if files:
        command = ["docker", "exec", "-i", container, "tar", "-C", "/", "-cf", "-",
                   "--no-recursion", "-T", "-"]
        tarred = run(command, input=_tar_listing(files))
        if tarred.returncode != 0:
            command = ["docker", "exec", "-i", container, "tar", "-C", "/", "-cf", "-", "-T", "-"]
            tarred = run(command, input=_tar_listing(files))
        if tarred.returncode != 0:
            raise RuntimeError("container tar failed: " + tarred.stderr.decode(errors="replace").strip())
        _extract(payload, tarred.stdout)

    previous_payload = Path(previous_dir) / "envdiff" if previous_dir else None
    record = _dedupe(payload, previous_payload, stats)
    step_record = dict(record)
    for relative, entry in previous_manifest.items():
        record.setdefault(relative, entry)
    manifest = {
        "container": container,
        "files": record,
        "step_files": step_record,
        "deletions": deletions,
        "counts": {
            "changed": len(files),
            "deleted": len(deletions),
            "stored": len(step_record),
            "linked": sum(1 for item in step_record.values() if item["linked"]),
        },
        "logical_bytes": sum(item["size"] for item in step_record.values()),
        "stored_bytes": sum(item["size"] for item in step_record.values() if not item["linked"]),
    }
    (out / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return manifest


def restore(container: str, delta_dir: str | Path, run: Runner = default_runner) -> dict:
    """Apply one environment delta to ``container``."""
    delta = Path(delta_dir)
    payload = delta / "envdiff"
    manifest_path = delta / MANIFEST
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {"files": {}, "deletions": []}
    applied = 0
    step_files = manifest.get("step_files") or manifest.get("files") or {}
    if payload.exists() and step_files:
        buffer = io.BytesIO()
        with tarfile.open(fileobj=buffer, mode="w") as archive:
            for relative in sorted(step_files):
                path = payload / relative
                if path.is_file() or path.is_symlink():
                    archive.add(path, arcname=relative)
        result = run(["docker", "exec", "-i", container, "tar", "-C", "/", "-xf", "-"],
                     input=buffer.getvalue())
        if result.returncode != 0:
            raise RuntimeError("restore tar failed: " + result.stderr.decode(errors="replace").strip())
        applied = len(step_files)
    for path in manifest.get("deletions", []):
        if not path.startswith("/") or path == "/":
            continue
        run(["docker", "exec", container, "rm", "-rf", "--", path])
    return {"applied": applied, "deleted": len(manifest.get("deletions", []))}


def _masked_environment(raw: str) -> dict[str, object]:
    values: dict[str, object] = {}
    for line in raw.splitlines():
        name, _, value = line.partition("=")
        if not name:
            continue
        if SECRET_PATTERN.search(name):
            values[name] = {"value_sha256": hashlib.sha256(value.encode()).hexdigest()}
        else:
            values[name] = value
    return values


def record_environment(container: str, out_dir: str | Path, run: Runner = default_runner) -> dict:
    """Record variables, shell files and package fingerprints for restore checks."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    env_result = run(["docker", "exec", container, "env"])
    environment = _masked_environment(env_result.stdout.decode(errors="replace")) if env_result.returncode == 0 else {}
    shell_files: list[str] = []
    for candidate in ("/root/.bashrc", "/root/.profile", "/etc/profile"):
        check = run(["docker", "exec", container, "test", "-f", candidate])
        if check.returncode == 0:
            shell_files.append(candidate)
    fingerprints: dict[str, str | None] = {}
    probes = {
        "pip": ["python", "-m", "pip", "freeze"],
        "dpkg": ["dpkg", "-l"],
        "npm": ["npm", "ls", "-g", "--json"],
    }
    for name, command in probes.items():
        result = run(["docker", "exec", container, *command])
        fingerprints[name] = (hashlib.sha256(result.stdout).hexdigest()
                               if result.returncode == 0 else None)
    document = {
        "environment": environment,
        "secret_names": sorted(values for values in environment
                               if isinstance(environment.get(values), dict)),
        "shell_files": shell_files,
        "fingerprints": fingerprints,
    }
    (out / ENVIRONMENT).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n")
    return document


def verify(container: str, delta_dir: str | Path, run: Runner = default_runner) -> dict:
    """Compare recorded fingerprints against the container's current state."""
    delta = Path(delta_dir)
    document = json.loads((delta / ENVIRONMENT).read_text())
    mismatches: list[str] = []
    probes = {
        "pip": ["python", "-m", "pip", "freeze"],
        "dpkg": ["dpkg", "-l"],
        "npm": ["npm", "ls", "-g", "--json"],
    }
    for name, expected in document.get("fingerprints", {}).items():
        if expected is None:
            continue
        result = run(["docker", "exec", container, *probes[name]])
        actual = hashlib.sha256(result.stdout).hexdigest() if result.returncode == 0 else None
        if actual != expected:
            mismatches.append(name)
    env_result = run(["docker", "exec", container, "env"])
    masked = _masked_environment(env_result.stdout.decode(errors="replace"))
    for name, expected in document.get("environment", {}).items():
        if name not in masked:
            mismatches.append(f"env:{name}")
    return {"verified": not mismatches, "mismatches": mismatches}
