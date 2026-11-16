from __future__ import annotations

import json
import gzip
import hashlib
import os
import re
import shlex
from pathlib import Path
from typing import Any
from uuid import uuid4


async def capture_harness_run(
    environment: Any,
    context: Any,
    *,
    workspace: str,
    logs_dir: Path,
) -> None:
    """Best-effort capture that must not mask the Agent's original outcome."""
    logs_dir.mkdir(parents=True, exist_ok=True)
    errors: list[str] = []
    summary_text = await _capture_text(
        environment,
        f"{workspace}/.harness/run_summary.json",
        logs_dir / "harness-run-summary.json",
        errors,
    )

    summary = _json_object(summary_text)
    trajectory_path = summary.get("trajectory_path") or f"{workspace}/.harness/trajectory.jsonl"
    path = Path(str(trajectory_path))
    if not path.is_absolute() or ".." in path.parts or not path.is_relative_to(Path(workspace) / ".harness"):
        errors.append("refused trajectory path outside workspace audit directory")
        trajectory_path = f"{workspace}/.harness/trajectory.jsonl"
    trajectory_text = await _capture_text(environment, str(trajectory_path),
        logs_dir / "harness-trajectory.jsonl", errors)
    api_capture = await _capture_api_audit(environment, Path(str(trajectory_path)).parent,
                                         trajectory_text or "", logs_dir, errors)
    checkpoint_capture = await _capture_checkpoints(environment, workspace, logs_dir, errors)
    usage = summary.get("task_usage") or summary.get("usage") or {}
    if isinstance(usage, dict):
        context.n_input_tokens = _optional_int(usage.get("input_tokens"))
        context.n_cache_tokens = _optional_int(usage.get("cache_read_tokens"))
        context.n_output_tokens = _optional_int(usage.get("output_tokens"))
        context.cost_usd = _optional_float(usage.get("cost_usd"))

    events: list[dict[str, Any]] = []
    lines = trajectory_text.splitlines() if trajectory_text else []
    for line in lines[-1000:]:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(event, dict):
            continue
        bounded = dict(event)
        bounded["output"] = str(bounded.get("output", ""))[:2000]
        events.append(bounded)

    context.metadata = {
        **(context.metadata or {}),
        "harness_trajectory": events,
        "harness_run_summary": summary,
        "harness_api_audit": api_capture,
        "harness_checkpoints": checkpoint_capture,
        "harness_trajectory_capture": {
            "total_lines": len(lines),
            "captured_events": len(events),
            "truncated": len(lines) > 1000,
            "raw_log": "harness-trajectory.jsonl" if trajectory_text else None,
            "summary_log": "harness-run-summary.json" if summary_text else None,
            "errors": errors,
        },
    }


async def _capture_checkpoints(environment: Any, workspace: str, logs_dir: Path,
                               errors: list[str]) -> dict[str, Any]:
    """Archive the opt-in boundary checkpoint directory next to the audit logs.

    The agent may write checkpoints under the task workspace or elsewhere; the
    directory is resolved exactly as main.py resolves it and copied out only
    when the opt-in environment variable is set.
    """
    control = (os.getenv("SCIENCE_CHECKPOINT_CONTROL_DIR") or "").strip()
    if not control:
        return {"configured": False}
    resolved = control.replace("{workspace}", workspace).replace("${WORKSPACE}", workspace)
    if not resolved.startswith("/"):
        resolved = workspace.rstrip("/") + "/" + resolved
    parent, _, name = resolved.rstrip("/").rpartition("/")
    if not parent or not name or ".." in Path(resolved).parts:
        errors.append("checkpoint control directory is invalid")
        return {"configured": True, "status": "invalid"}
    archive = f"/tmp/.harness-checkpoints-{uuid4().hex}.tgz"
    try:
        result = await environment.exec(
            command=f"tar -czf {shlex.quote(archive)} -C {shlex.quote(parent)} {shlex.quote(name)}")
    except Exception as exc:
        errors.append(f"checkpoint archive: {type(exc).__name__}: {exc}")
        return {"configured": True, "status": "failed", "path": resolved}
    if result.return_code != 0:
        errors.append("checkpoint archive: directory is unavailable")
        return {"configured": True, "status": "missing", "path": resolved}
    target = logs_dir / "harness-checkpoints.tar.gz"
    try:
        await environment.download_file(archive, target)
    except Exception as exc:
        errors.append(f"checkpoint download: {type(exc).__name__}: {exc}")
        return {"configured": True, "status": "failed", "path": resolved}
    finally:
        try:
            await environment.exec(command=f"rm -f {shlex.quote(archive)}")
        except Exception:
            pass
    return {"configured": True, "status": "captured", "path": resolved,
            "bytes": target.stat().st_size, "log": target.name}


async def _capture_api_audit(environment, session_dir, trajectory, logs_dir, errors):
    references = set()
    invalid_reference = False
    for line in trajectory.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(event, dict) and event.get("type") in {"api_request", "api_response", "api_error"}:
            reference = event.get("payload_ref", "")
            if not isinstance(reference, str) or not re.fullmatch(r"api/objects/[0-9a-f]{64}\.json\.gz", reference):
                errors.append("invalid API audit reference")
                invalid_reference = True
                continue
            references.add(reference)
    if not references:
        return {"payloads": 0, "objects": 0, "complete": not invalid_reference}
    destination = logs_dir / "api" / "objects"
    destination.mkdir(parents=True, exist_ok=True)
    download_dir = getattr(environment, "download_dir", None)
    if callable(download_dir):
        try:
            await download_dir(str(session_dir / "api"), logs_dir / "api")
        except Exception:
            pass  # Fall back to exact, validated object downloads below.
    captured = set()

    async def fetch(digest):
        if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid API object digest")
        local = destination / f"{digest}.json.gz"
        if not local.is_file():
            await environment.download_file(str(session_dir / "api" / "objects" / local.name), local)
        raw = gzip.decompress(local.read_bytes())
        if hashlib.sha256(raw).hexdigest() != digest:
            raise ValueError("API object checksum mismatch")
        captured.add(digest)
        return json.loads(raw)

    complete = not invalid_reference
    for reference in sorted(references):
        try:
            manifest = await fetch(Path(reference).name[:-8])
            if manifest.get("version") != 1:
                raise ValueError("unsupported API audit format")
            dependencies = ([manifest["value"]] if manifest["kind"] == "value" else
                            [digest for part in manifest["parts"].values()
                             for digest in (part["items"] if "items" in part else [part["value"]])])
            for digest in dependencies:
                if digest not in captured:
                    await fetch(digest)
        except Exception as exc:
            complete = False
            errors.append(f"API audit export: {type(exc).__name__}: {exc}")
    return {"payloads": len(references), "objects": len(captured), "complete": complete,
            "directory": "api", "format": "json-parts-v1"}


async def _capture_text(
    environment: Any,
    remote_path: str,
    local_path: Path,
    errors: list[str],
) -> str:
    try:
        await environment.download_file(remote_path, local_path)
        return local_path.read_text(encoding="utf-8")
    except Exception as download_error:
        errors.append(
            f"download {Path(remote_path).name}: "
            f"{type(download_error).__name__}: {download_error}"
        )
    try:
        result = await environment.exec(
            command=f"cat {shlex.quote(remote_path)}"
        )
    except Exception as read_error:
        errors.append(
            f"read {Path(remote_path).name}: "
            f"{type(read_error).__name__}: {read_error}"
        )
        return ""
    if result.return_code != 0 or not result.stdout:
        errors.append(f"read {Path(remote_path).name}: unavailable")
        return ""
    return result.stdout


def _json_object(value: str) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _optional_int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _optional_float(value: Any) -> float | None:
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None
