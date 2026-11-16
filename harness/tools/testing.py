from __future__ import annotations

import time
import hashlib
from typing import Any

from .base import ArtifactRef, StateDelta, ToolContext, ToolResult, VerificationEvidence
from .repository import discover_check_commands
from .filesystem import resolve_workspace
from .shell import run_command


def discover_checks(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        root = resolve_workspace(context, str(args.get("path", ".")))
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    executor = context.executor
    assert executor is not None
    checks = discover_check_commands(root, executor)
    text = "\n".join(
        f"- {item['command']}  # {item['source']}" for item in checks
    )
    return ToolResult(
        True,
        f"discovered {len(checks)} candidate checks",
        stdout=text or "[no project-native checks discovered]",
    )


def run_check(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    command = str(args.get("command", "")).strip()
    target = str(args.get("target", "")).strip()
    if not command or not target:
        return ToolResult(
            False,
            "command and target are required",
            failure_kind="invalid_input",
        )
    executor = context.executor
    assert executor is not None
    before = executor.snapshot(context.workspace)
    started_at = time.time()
    result = run_command(context, args)
    after = executor.snapshot(context.workspace)
    delta = StateDelta(
        created=tuple(sorted(after.keys() - before.keys())),
        modified=tuple(sorted(
            path for path in after.keys() & before.keys()
            if after[path] != before[path]
        )),
        deleted=tuple(sorted(before.keys() - after.keys())),
    )
    changed = delta.created or delta.modified or delta.deleted
    evidence = {
        "target": target,
        "command": command,
        "cwd": str(args.get("cwd", ".")),
        "ok": result.ok,
        "exit_code": result.exit_code,
        "started_at": started_at,
        "completed_at": time.time(),
    }
    evidence_model = VerificationEvidence(
        target=target, command=command, passed=result.ok,
        exit_code=result.exit_code,
        duration_seconds=max(0.0, time.time() - started_at),
        artifacts=tuple(ArtifactRef(path) for path in result.artifacts),
        check_type="command",
        details={"cwd": str(args.get("cwd", "."))},
        artifact_hash=hashlib.sha256((result.stdout or "").encode()).hexdigest(),
    )
    return ToolResult(
        result.ok,
        (
            f"verification {'passed' if result.ok else 'failed'} for {target}"
            + (
                "; verification changed the workspace—remove temporary "
                "artifacts or restore unintended changes before completion"
                if changed else ""
            )
        ),
        stdout=result.stdout,
        stderr=result.stderr,
        exit_code=result.exit_code,
        artifacts=result.artifacts,
        failure_kind=result.failure_kind,
        retryable=result.retryable,
        state_delta=delta if changed else None,
        verification=evidence,
        verification_evidence=evidence_model,
        structured={"verification_evidence": evidence_model.dictionary()},
    )
