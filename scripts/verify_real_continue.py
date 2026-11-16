#!/usr/bin/env python3
"""Restore one checkpoint with the real model and continue the original task."""
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
from harness.model import OpenAICompatibleModel
from harness.protocol import Shutdown, UserTurn
from harness.spec import build_spec
from harness.trusted.deployment import launcher_executor, launcher_permission_policy, launcher_sandbox
from checkpoint_plugin.adapters.model_compatibility import restore_model


def to_snapshot(source: Path, target: Path) -> None:
    request = json.loads((source / "request.json").read_text())
    target.mkdir(parents=True, exist_ok=True)
    (target / "request.json").symlink_to(source / "request.json")
    for name in request["components"]:
        (target / f"component_{name}").symlink_to(source / "components" / name)
    packed = False
    for label, record in request.get("archives", {}).items():
        kind = record.get("kind")
        if kind == "missing":
            continue
        if kind == "packed":
            if not packed:
                (target / "archives.json").symlink_to(source / "archives" / record["path"])
                packed = True
            continue
        path = source / "archives" / (record.get("path") or label)
        (target / f"archive_{label}").symlink_to(path)
    pack = source.parent / ".archive-pack"
    if pack.is_dir() and not (target.parent / ".archive-pack").exists():
        (target.parent / ".archive-pack").symlink_to(pack)


async def run_turn(session, runner, timeout: float):
    loop = session.open_submission_loop(runner)
    consumer = asyncio.create_task(loop.run())
    try:
        loop.submit(UserTurn(session_id=session.session_id, turn_id="resume",
                             operation_id="resume-start", text=session.task.text))
        result = await asyncio.wait_for(loop.wait_result("resume"), timeout)
        return result
    finally:
        loop.submit(Shutdown(session_id=session.session_id, operation_id="resume-stop"))
        await asyncio.wait_for(consumer, 30)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--workspace", default="/")
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()

    os.environ.setdefault("HARNESS_EXECUTOR", "contained")
    os.environ.setdefault("SCIENCE_CHECKPOINT_CONTROL_DIR", "/tmp/ctrl")

    target = None
    for request_path in args.root.rglob("request.json"):
        if request_path.parent.name.startswith(args.checkpoint):
            target = request_path.parent
            break
    if target is None:
        print("checkpoint not found")
        return 1
    request = json.loads((target / "request.json").read_text())
    print(f"checkpoint {target.name} sampling={(request['boundary']).get('sampling_index')} "
          f"trigger={request.get('trigger')}", flush=True)

    with tempfile.TemporaryDirectory(prefix="snap-") as temp:
        snapshot = Path(temp) / "snap"
        to_snapshot(target, snapshot)
        spec = build_spec(ROOT, Path(args.workspace))
        thinking = os.getenv("MODEL_THINKING", "auto").strip().lower()
        model = OpenAICompatibleModel.from_config(spec).configure_generation(
            thinking_type=None if thinking == "auto" else thinking,
            reasoning_effort=os.getenv("MODEL_REASONING_EFFORT", "high"))
        control = Path(os.environ["SCIENCE_CHECKPOINT_CONTROL_DIR"])
        from checkpoint_plugin.adapters.agent_state import HarnessStateAdapter
        from science_checkpoint.request_tool import boundary_factory as checkpoint_factory
        factory = checkpoint_factory(HarnessStateAdapter(spec).freeze, control, wait_seconds=0)
        policy = launcher_permission_policy("container-full-access")
        sandbox = launcher_sandbox("container-full-access")
        runtime = AgentRuntime(spec, model, executor=launcher_executor("container-full-access", spec.workspace),
                               permission_policy=policy, sandbox=sandbox, boundary_factory=factory)
        importer = HarnessStateImporter(model_validator=restore_model, planning_assembler=None)
        imported = importer.restore_snapshot(snapshot, runtime)
        session = imported.session
        print(f"restored messages={len(session.messages)} events={len(session.events)}", flush=True)

        baseline = len(session.events)
        try:
            result = asyncio.run(run_turn(session, imported.runner, args.timeout))
        except Exception as exc:
            new_events = session.events[baseline:]
            print(json.dumps({
                "outcome": "TIMEOUT" if isinstance(exc, asyncio.TimeoutError) else "ERROR",
                "error": f"{type(exc).__name__}: {exc}"[:200],
                "new_api_requests": sum(1 for e in new_events if e.get("type") == "api_request"),
                "new_tool_events": sum(1 for e in new_events if e.get("type") == "tool"),
                "new_tool_failures": sum(1 for e in new_events
                                          if e.get("type") == "tool" and e.get("ok") is False),
                "failure_samples": [str(e.get("output"))[:120] for e in new_events
                                    if e.get("type") == "tool" and e.get("ok") is False][:3],
                "assistant_suspicious": [e.get("text", "")[:160] for e in new_events
                                         if e.get("type") == "assistant_message"
                                         and any(k in e.get("text", "").lower() for k in (
                                             "not found", "unavailable", "mismatch", "environment",
                                             "cannot", "reset", "missing", "permission"))][:3],
            }, ensure_ascii=False))
            return 0
        new_events = session.events[baseline:]
        failures = [e for e in new_events if e.get("type") == "tool" and e.get("ok") is False]
        assistant = [e.get("text", "") for e in new_events if e.get("type") == "assistant_message"]
        keywords = ("not found", "no such file", "unavailable", "mismatch", "environment",
                    "cannot", "failed", "error", "reset", "missing", "permission")
        suspicious = [text[:160] for text in assistant if any(k in text.lower() for k in keywords)]
        tools = {}
        for e in new_events:
            if e.get("type") == "tool":
                tools[e.get("name")] = tools.get(e.get("name"), 0) + 1
        print(json.dumps({
            "tool_failures": len(failures),
            "failure_samples": [str(e.get("output"))[:120] for e in failures[:3]],
            "tools": tools,
            "assistant_suspicious": suspicious[:3],
        }, ensure_ascii=False))
        print(json.dumps({
            "outcome": result.outcome.name,
            "error": str(getattr(result, "error", "") or "")[:200],
            "new_api_requests": sum(1 for e in new_events if e.get("type") == "api_request"),
            "new_tool_events": sum(1 for e in new_events if e.get("type") == "tool"),
            "final_chars": len(result.last_agent_message or ""),
            "final_preview": (result.last_agent_message or "")[:200],
        }, ensure_ascii=False))
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
