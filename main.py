from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from harness.agent import CodingAgent
from harness.model import OpenAICompatibleModel
from harness.spec import build_spec
from harness.trusted.deployment import (
    PERMISSION_PROFILES, launcher_permission_policy, launcher_sandbox, launcher_executor,
)


ROOT = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(description="Minimal coding harness")
    parser.add_argument("task", nargs="?")
    parser.add_argument("--inspect", action="store_true")
    parser.add_argument("--engine", choices=("turn",), default="turn")
    parser.add_argument("--permission-profile", choices=PERMISSION_PROFILES,
                        default=os.getenv("HARNESS_PERMISSION_PROFILE", "restricted"),
                        help="container-full-access disables inner approvals/path sandbox; trusted containers only")
    mode = parser.add_mutually_exclusive_group()
    parser.add_argument("--resume", help="checkpoint directory to import and continue")
    parser.add_argument("--resume-timeout", type=float, default=3600.0)
    mode.add_argument("--interactive", action="store_true")
    mode.add_argument("--headless", action="store_true")
    args = parser.parse_args()
    if args.permission_profile != "restricted" and args.engine != "turn":
        parser.error("container permission profiles are only supported by the turn engine")
    try:
        permission_policy = launcher_permission_policy(args.permission_profile)
        sandbox = launcher_sandbox(args.permission_profile)
    except ValueError as exc:
        parser.error(str(exc))
    if args.permission_profile == "container-full-access":
        print("WARNING: inner approvals and path sandbox disabled; relying on the outer container.",
              file=sys.stderr)
    spec = build_spec(ROOT)
    if args.inspect:
        print(json.dumps(spec.inspect(), indent=2, ensure_ascii=False))
        return
    if not args.task and not args.resume:
        parser.error("provide a task")
    if args.interactive and (args.engine != "turn" or not sys.stdin.isatty()):
        parser.error("--interactive requires --engine turn and a terminal stdin")
    # Enable task reasoning by default. "auto" omits this provider-specific
    # extension for gateways which select thinking through another mechanism.
    thinking = os.getenv("MODEL_THINKING", "enabled").strip().lower()
    model = OpenAICompatibleModel.from_config(spec).configure_generation(
        thinking_type=None if thinking == "auto" else thinking,
        reasoning_effort=os.getenv("MODEL_REASONING_EFFORT", "high"),
    )
    result = None
    failure = None
    agent = None
    control_dir = os.getenv("SCIENCE_CHECKPOINT_CONTROL_DIR")
    boundary_factory = None
    if control_dir:
        # S1a is opt-in and only writes a typed boundary snapshot; it does not
        # change normal execution when the environment variable is absent.
        # Accept "{workspace}" (or a relative path) so one configuration works
        # for tasks whose agent workspace differs.
        from pathlib import Path as _Path
        control_dir = control_dir.replace('{workspace}', str(spec.workspace)).replace(
            '${WORKSPACE}', str(spec.workspace))
        control_path = _Path(control_dir)
        if not control_path.is_absolute():
            control_path = spec.workspace / control_path
        from checkpoint_plugin.adapters.agent_state import HarnessStateAdapter
        from science_checkpoint.request_tool import boundary_factory as checkpoint_factory
        from science_checkpoint.policy import CheckpointPolicyConfig
        wait = float(os.getenv("SCIENCE_CHECKPOINT_ACK_TIMEOUT", "0") or "0")
        if wait < 0:
            parser.error("SCIENCE_CHECKPOINT_ACK_TIMEOUT must be nonnegative")
        checkpoint_config = CheckpointPolicyConfig(
            minimum_interval_seconds=float(os.getenv('SCIENCE_CHECKPOINT_MIN_INTERVAL_SECONDS', '120')),
            fallback_interval_seconds=float(os.getenv('SCIENCE_CHECKPOINT_FALLBACK_INTERVAL_SECONDS', '900')))
        boundary_factory = checkpoint_factory(HarnessStateAdapter(spec).freeze, control_path,
            wait_seconds=wait, config=checkpoint_config)
    if args.resume:
        from harness.core.runtime import AgentRuntime
        from harness.resume import resume_checkpoint
        runtime = AgentRuntime(
            spec, model, executor=launcher_executor(args.permission_profile, spec.workspace),
            permission_policy=permission_policy, sandbox=sandbox,
            boundary_factory=boundary_factory)
        try:
            result = asyncio.run(resume_checkpoint(runtime, Path(args.resume), timeout=args.resume_timeout))
        except asyncio.TimeoutError:
            print(json.dumps({"outcome": "timeout", "error": "resume timeout"}))
            return
        print(result.last_agent_message or "")
        print(json.dumps({"outcome": result.outcome.name, "error": result.error,
                          "session_id": result.session_id}))
        return
    try:
        agent = (CodingAgent(spec, model, boundary_factory=boundary_factory) if permission_policy is None else
                 CodingAgent(spec, model, permission_policy=permission_policy, sandbox=sandbox,
                             executor=launcher_executor(args.permission_profile, spec.workspace),
                             boundary_factory=boundary_factory))
        if args.interactive:
            result = asyncio.run(_interactive(agent, args.task))
        else:
            result = asyncio.run(agent.run_async(args.task))
    except Exception as exc:
        failure = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        _write_run_summary(spec.workspace, model, result, failure,
                           session=getattr(agent.runtime, "last_session", None) if agent is not None else None,
                           is_turn=args.engine == "turn")
    assert result is not None
    if not args.interactive:
        print(result.text)
    if not result.verified:
        print("\n[verification not evaluated; external evaluator decides]" if args.engine == "turn"
              else "\n[completion not verified]")
        for reason in result.reasons:
            print(f"- {reason}")
    if args.engine == "turn":
        last = next((e for e in reversed(result.events) if e.get("type") == "run_end"), {})
        status = {"error": 1, "budget_limited": 2, "interrupted": 130}.get(last.get("termination"), 0)
        if status:
            raise SystemExit(status)


async def _interactive(agent: CodingAgent, task: str):
    from harness.frontends import Conversation, TerminalFrontend, TerminalInput

    terminal = TerminalInput(sys.stdin, sys.stdout)
    frontend = TerminalFrontend(terminal.readline, print)
    async with Conversation(agent.runtime, task, handler=frontend) as conversation:
        while True:
            result = await conversation.turn(task)
            print(result.text)
            task = await terminal.readline("\nNext request (/exit to close): ")
            if task is None or task.strip() == "/exit":
                return result
            while not task.strip():
                task = await terminal.readline("Next request: ")
                if task is None or task.strip() == "/exit":
                    return result


def _write_run_summary(workspace, model, result, failure, *, session=None, is_turn=False) -> None:
    output_dir = workspace / ".harness"
    output_dir.mkdir(parents=True, exist_ok=True)
    result_events = result.events if result is not None else (session.events if session is not None else [])
    persisted_events = ([] if is_turn else _read_trajectory(output_dir / "trajectory.jsonl"))
    run_end = next((
        event for event in reversed(result_events)
        if event.get("type") == "run_end"
    ), None) or next((
        event for event in reversed(persisted_events)
        if event.get("type") == "run_end"
    ), {})
    value = {
        "model": model.model,
        "reasoning_effort": getattr(model, "reasoning_effort", None),
        "thinking_type": getattr(model, "thinking_type", None),
        "parallel_tool_calls": getattr(model, "parallel_tool_calls", None),
        "usage": model.usage_snapshot(),
        "task_usage": run_end.get("task_usage"),
        "verified": result.verified if result is not None else False,
        "reasons": list(result.reasons) if result is not None else [],
        "failure": failure,
        "event_count": len(result_events) or len(persisted_events),
        "termination": run_end.get(
            "termination", "exception" if failure else "unknown"
        ),
        "verification_status": run_end.get("verification_status", "legacy"),
        "execution_path": run_end.get("execution_path", "legacy"),
        "trajectory_path": next((e.get("trajectory_path") for e in reversed(result_events)
                                  if e.get("type") == "run_start"), None),
        "behavior_summary": run_end.get("behavior_summary", {}),
        "workspace_manifest": _workspace_manifest(workspace),
    }
    destination = output_dir / "run_summary.json"
    temporary = output_dir / f"run_summary.{os.getpid()}.tmp"
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2))
    temporary.replace(destination)


def _read_trajectory(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    values = []
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return []
    for line in lines[-1000:]:
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            values.append(value)
    return values


def _workspace_manifest(workspace: Path, limit: int = 2000) -> list[dict]:
    values = []
    try:
        paths = sorted(
            path for path in workspace.rglob("*")
            if path.is_file() and ".harness" not in path.parts
        )
    except OSError:
        return values
    for path in paths[:limit]:
        try:
            stat = path.stat()
            record = {
                "path": str(path.relative_to(workspace)),
                "size": stat.st_size,
            }
            if stat.st_size <= 1_000_000:
                record["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
            values.append(record)
        except OSError:
            continue
    return values


if __name__ == "__main__":
    main()
