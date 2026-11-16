#!/usr/bin/env python3
"""Verify that a captured checkpoint imports back into a live harness runtime.

Usage:
    verify_step_restore.py --archive path/harness-checkpoints.tar.gz [--checkpoint ID]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from checkpoint_plugin.adapters.agent_import import HarnessStateImporter
from harness.core.runtime import AgentRuntime
from harness.spec import build_spec
from harness.trusted.deployment import launcher_executor, launcher_permission_policy, launcher_sandbox
from tests.test_turn_loop import Model, response


def extract(archive: Path, target: Path) -> Path:
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="tar")
    roots = list(target.rglob("request.json"))
    if not roots:
        raise SystemExit("no request.json inside archive")
    return target


def pick(checkpoint_root: Path, requested: str | None) -> Path:
    candidates = []
    for request_path in checkpoint_root.rglob("request.json"):
        directory = request_path.parent
        try:
            request = json.loads(request_path.read_text())
        except ValueError:
            continue
        trigger = str(request.get("trigger") or "")
        sampling = (request.get("boundary") or {}).get("sampling_index", -1)
        candidates.append((sampling, trigger, directory))
    if requested:
        for _, _, directory in candidates:
            if directory.name.startswith(requested):
                return directory
        raise SystemExit("requested checkpoint not found")
    steps = [item for item in candidates if item[1] == "step"]
    if not steps:
        raise SystemExit("no step checkpoint found")
    return max(steps)[2]


def to_snapshot_layout(source: Path, target: Path) -> dict:
    target.mkdir(parents=True, exist_ok=True)
    request = json.loads((source / "request.json").read_text())
    shutil.copy2(source / "request.json", target / "request.json")
    for name in request["components"]:
        shutil.copy2(source / "components" / name, target / f"component_{name}")
    for label, record in request.get("archives", {}).items():
        if record.get("kind") == "missing":
            continue
        name = record.get("path") or (label + ".bin")
        source_path = source / "archives" / name
        if record.get("kind") == "packed":
            shutil.copy2(source_path, target / "archives.json")
            pack_source = source.parent / ".archive-pack"
            if pack_source.is_dir():
                shutil.copytree(pack_source, target.parent / ".archive-pack", dirs_exist_ok=True)
            continue
        if source_path.is_file():
            shutil.copy2(source_path, target / f"archive_{label}")
    return request


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--checkpoint")
    parser.add_argument("--workspace", type=Path, help="in-environment workspace recorded in the snapshot")
    parser.add_argument("--permission-profile", default="container-full-access")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        checkpoint_root = extract(args.archive, work / "archive")
        chosen = pick(checkpoint_root, args.checkpoint)
        request = json.loads((chosen / "request.json").read_text())
        print("checkpoint", chosen.name, "trigger", request.get("trigger"),
              "components", len(request["components"]), "archives", len(request.get("archives", {})))
        snap = work / "snapshot"
        to_snapshot_layout(chosen, snap)

        import os
        if args.workspace:
            os.environ["HARNESS_WORKSPACE"] = str(args.workspace)
        spec = build_spec(ROOT, args.workspace or (work / "workspace"))
        model = Model([response(text="restored and ready")])
        window = os.environ.get("MODEL_CONTEXT_WINDOW_TOKENS")
        reserve = os.environ.get("MODEL_OUTPUT_RESERVE_TOKENS")
        if window:
            model.context_window_tokens = int(window)
        if reserve:
            model.output_reserve_tokens = int(reserve)
        factory = None
        control = os.environ.get("SCIENCE_CHECKPOINT_CONTROL_DIR")
        if control:
            from checkpoint_plugin.adapters.agent_state import HarnessStateAdapter
            from science_checkpoint.request_tool import boundary_factory as checkpoint_factory
            factory = checkpoint_factory(HarnessStateAdapter(spec).freeze, Path(control), wait_seconds=0)
        policy = launcher_permission_policy(args.permission_profile)
        sandbox = launcher_sandbox(args.permission_profile)
        runtime = AgentRuntime(spec, model, executor=launcher_executor(args.permission_profile, spec.workspace),
                               permission_policy=policy, sandbox=sandbox, boundary_factory=factory)
        importer = HarnessStateImporter(model_validator=lambda provider, target_model, session_id: True,
            planning_assembler=None)
        try:
            imported = importer.restore_snapshot(snap, runtime)
        except Exception as exc:
            print("RESTORE_FAILED", type(exc).__name__, str(exc)[:400])
            return 1
        session = imported.session
        print("RESTORE_OK messages", len(session.messages),
              "events", len(session.events),
              "task_chars", len(session.task.text or ""),
              "runner", type(imported.runner).__name__)
        print("last_role", session.messages[-1].get("role") if session.messages else None)
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
