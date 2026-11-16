#!/usr/bin/env python3
"""Restore every checkpoint in a control directory and continue one turn.

Run inside the task image (same workspace and env as capture):
    verify_all_checkpoints.py --root /ckpt/.checkpoints --workspace /app --continue
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from checkpoint_plugin.adapters.agent_import import HarnessStateImporter
from harness.core.runtime import AgentRuntime
from harness.core.turn import TurnLoopDriver
from harness.protocol import Shutdown, UserTurn
from harness.spec import build_spec
from harness.tasks import RegularTask
from harness.trusted.deployment import launcher_executor, launcher_permission_policy, launcher_sandbox
from tests.test_turn_loop import Model, response


def to_snapshot(source: Path, target: Path) -> None:
    request = json.loads((source / "request.json").read_text())
    target.mkdir(parents=True, exist_ok=True)
    (target / "request.json").symlink_to(source / "request.json")
    for name in request["components"]:
        (target / f"component_{name}").symlink_to(source / "components" / name)
    for label, record in request.get("archives", {}).items():
        kind = record.get("kind")
        if kind == "missing":
            continue
        if kind == "packed":
            if not (target / "archives.json").exists():
                (target / "archives.json").symlink_to(source / "archives" / record["path"])
            continue
        path = source / "archives" / (record.get("path") or label)
        (target / f"archive_{label}").symlink_to(path)
    pack = source.parent / ".archive-pack"
    if pack.is_dir() and not (target.parent / ".archive-pack").exists():
        (target.parent / ".archive-pack").symlink_to(pack)


def build_runtime(workspace: Path, model: Model) -> AgentRuntime:
    from checkpoint_plugin.adapters.agent_state import HarnessStateAdapter
    from science_checkpoint.request_tool import boundary_factory as checkpoint_factory
    model.context_window_tokens = int(os.environ.get("MODEL_CONTEXT_WINDOW_TOKENS", "350000"))
    model.output_reserve_tokens = int(os.environ.get("MODEL_OUTPUT_RESERVE_TOKENS", "64000"))
    spec = build_spec(ROOT, workspace)
    control = Path(os.environ.get("SCIENCE_CHECKPOINT_CONTROL_DIR", "/tmp/ctrl"))
    factory = checkpoint_factory(HarnessStateAdapter(spec).freeze, control, wait_seconds=0)
    policy = launcher_permission_policy("container-full-access")
    sandbox = launcher_sandbox("container-full-access")
    return AgentRuntime(spec, model, executor=launcher_executor("container-full-access", spec.workspace),
                        permission_policy=policy, sandbox=sandbox, boundary_factory=factory)


async def continue_once(session, runner, turn: str) -> str:
    loop = session.open_submission_loop(runner)
    consumer = asyncio.create_task(loop.run())
    try:
        loop.submit(UserTurn(session_id=session.session_id, turn_id=turn,
                             operation_id="continue-" + turn, text=session.task.text))
        result = await asyncio.wait_for(loop.wait_result(turn), 60)
        return f"{result.outcome.name}:{str(getattr(result, 'error', '') or '')[:120]}"
    finally:
        loop.submit(Shutdown(session_id=session.session_id, operation_id="stop-" + turn))
        await asyncio.wait_for(consumer, 30)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--workspace", default="/")
    parser.add_argument("--continue", dest="resume", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()

    os.environ.setdefault("HARNESS_EXECUTOR", "contained")
    if not os.environ.get("SCIENCE_CHECKPOINT_CONTROL_DIR"):
        os.environ["SCIENCE_CHECKPOINT_CONTROL_DIR"] = "/tmp/ctrl"

    checkpoints = []
    for request_path in args.root.rglob("request.json"):
        try:
            request = json.loads(request_path.read_text())
        except ValueError:
            continue
        checkpoints.append((request.get("boundary", {}).get("sampling_index", 0),
                            request.get("trigger"), request_path.parent))
    checkpoints.sort()
    if args.only:
        checkpoints = [item for item in checkpoints
                       if any(item[2].name.startswith(prefix) for prefix in args.only)]
    if args.limit:
        checkpoints = checkpoints[:args.limit]

    failures = []
    for index, (sampling, trigger, directory) in enumerate(checkpoints):
        with tempfile.TemporaryDirectory(prefix="snap-") as temp:
            snapshot = Path(temp) / "snap"
            try:
                to_snapshot(directory, snapshot)
                model = Model([response(text="continued")])
                runtime = build_runtime(Path(args.workspace), model)
                imported = HarnessStateImporter(model_validator=lambda provider, target, session_id: True,
                    planning_assembler=None).restore_snapshot(snapshot, runtime)
                outcome = "restored"
                if args.resume:
                    outcome = asyncio.run(continue_once(imported.session, imported.runner,
                                                        f"s{index}"))
                print(f"OK {directory.name} s={sampling} {trigger} outcome={outcome} "
                      f"model_calls={len(model.requests)}", flush=True)
            except Exception as exc:
                failures.append(directory.name)
                print(f"FAIL {directory.name} s={sampling} {trigger} "
                      f"{type(exc).__name__}: {str(exc)[:160]}", flush=True)
    print(f"summary total={len(checkpoints)} failures={len(failures)} {failures}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
