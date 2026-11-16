#!/usr/bin/env python3
"""Restore one checkpoint: apply file deltas, then import agent state.

Usage:
    restore_checkpoint.py --archive harness-checkpoints.tar.gz \
        --checkpoint <id-prefix> --image <task-image> [--workspace /app] [--keep]
"""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import tarfile
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(argv, input=None, check=False):
    result = subprocess.run(argv, input=input, capture_output=True, text=True)
    if check and result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(argv)}\n{result.stderr[-800:]}")
    return result


def apply_tree(container: str, payload: Path) -> None:
    """Stream files into the container as root with preserved mtimes."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        for path in sorted(payload.rglob("*")):
            archive.add(path, arcname=str(path.relative_to(payload)))
    subprocess.run(["docker", "exec", "-i", container, "tar", "-C", "/",
                    "--no-same-owner", "-xf", "-"], input=buffer.getvalue(), check=True)


def extract(archive: Path, target: Path) -> Path:
    with tarfile.open(archive) as tar:
        tar.extractall(target, filter="tar")
    return target


def checkpoints(root: Path) -> list[dict]:
    items = []
    for request_path in root.rglob("request.json"):
        directory = request_path.parent
        try:
            request = json.loads(request_path.read_text())
        except ValueError:
            continue
        boundary = request.get("boundary") or {}
        items.append({"directory": directory, "id": directory.name,
                      "sampling": boundary.get("sampling_index", 0),
                      "request": request})
    return sorted(items, key=lambda item: item["sampling"])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--checkpoint", required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--workspace", default="/")
    parser.add_argument("--platform")
    parser.add_argument("--keep", action="store_true")
    args = parser.parse_args()

    args.archive = args.archive.resolve()
    work = Path(tempfile.mkdtemp(prefix="restore-"))
    container = f"restore-{uuid.uuid4().hex[:10]}"
    try:
        root = extract(args.archive, work / "archive")
        items = checkpoints(root)
        target = next((item for item in items if item["id"].startswith(args.checkpoint)), None)
        if target is None:
            print("checkpoint not found", file=sys.stderr)
            return 1
        command = ["docker", "run", "-d", "--name", container, "-w", args.workspace,
                   "-v", f"{ROOT}:/installed-harness:ro", "-v", f"{args.archive}:/ckpt.tar.gz:ro"]
        if args.platform:
            command[3:3] = ["--platform", args.platform]
        command += [args.image, "sleep", "infinity"]
        run(command, check=True)

        applied = 0
        for item in items:
            if item["sampling"] > target["sampling"]:
                break
            env = item["directory"] / "env"
            payload = env / "envdiff"
            if payload.is_dir():
                apply_tree(container, payload)
                applied += 1
            deletions = env / "deletions.json"
            if deletions.is_file():
                paths = json.loads(deletions.read_text())
                if paths:
                    run(["docker", "exec", container, "rm", "-rf", "--", *paths])

        exec_env = ["-e", "HARNESS_EXECUTOR=contained",
                    "-e", "SCIENCE_CHECKPOINT_CONTROL_DIR=/tmp/ctrl",
                    "-e", "MODEL_CONTEXT_WINDOW_TOKENS=350000",
                    "-e", "MODEL_OUTPUT_RESERVE_TOKENS=64000"]
        result = run(["docker", "exec", *exec_env, "-w", args.workspace, container,
                      "python3", "/installed-harness/scripts/verify_step_restore.py",
                      "--archive", "/ckpt.tar.gz", "--workspace", args.workspace,
                      "--checkpoint", target["id"]])
        print(result.stdout[-2000:])
        if result.returncode != 0:
            print(result.stderr[-2000:], file=sys.stderr)
        print(f"env_layers={applied} checkpoint={target['id']} rc={result.returncode}")
        return result.returncode
    finally:
        if not args.keep:
            run(["docker", "rm", "-f", container])
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
