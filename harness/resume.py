"""Resume a captured checkpoint inside a live harness runtime.

The importer rebuilds the session and returns a ``RegularTask(restored=True)``
runner. Restored turns validate that the submitted text is the original task
and deliberately do not append it again, so driving the runner with the task
text continues the open turn instead of starting a fresh instruction.
"""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path

from checkpoint_plugin.adapters.agent_import import HarnessStateImporter
from harness.protocol import Shutdown, UserTurn
from checkpoint_plugin.adapters.model_compatibility import restore_model


def _to_snapshot(source: Path, target: Path) -> None:
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


async def resume_checkpoint(runtime, checkpoint: Path, *, timeout: float = 3600):
    """Import ``checkpoint`` and continue its open turn to completion."""
    session = None
    with tempfile.TemporaryDirectory(prefix="resume-") as temporary:
        snapshot = Path(temporary) / "snap"
        _to_snapshot(Path(checkpoint), snapshot)
        importer = HarnessStateImporter(model_validator=restore_model,
            planning_assembler=None)
        imported = importer.restore_snapshot(snapshot, runtime)
        session, runner = imported.session, imported.runner
        loop = session.open_submission_loop(runner)
        consumer = asyncio.create_task(loop.run())
        try:
            loop.submit(UserTurn(session_id=session.session_id, turn_id="resume",
                                 operation_id="resume-start", text=session.task.text))
            return await asyncio.wait_for(loop.wait_result("resume"), timeout)
        finally:
            loop.submit(Shutdown(session_id=session.session_id, operation_id="resume-stop"))
            await asyncio.wait_for(consumer, 30)
