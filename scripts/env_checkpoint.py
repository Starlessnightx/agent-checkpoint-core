#!/usr/bin/env python3
"""Capture and restore environment deltas for a running task container.

Subcommands:
    capture   --container C --out DIR [--previous DIR]
    restore   --container C --deltas D1 D2 ...
    env       --container C --out DIR
    verify    --container C --dir DELTA_DIR
    watch     --container C --control /control --out-root DIR
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from science_checkpoint import env_delta


def _capture(args) -> int:
    manifest = env_delta.capture(args.container, args.out, previous_dir=args.previous)
    if not args.no_environment:
        env_delta.record_environment(args.container, args.out)
    print(json.dumps({key: manifest[key] for key in ("counts", "logical_bytes", "stored_bytes")}))
    return 0


def _restore(args) -> int:
    for delta in args.deltas:
        print(json.dumps(env_delta.restore(args.container, delta)))
    return 0


def _env(args) -> int:
    document = env_delta.record_environment(args.container, args.out)
    print(json.dumps({"variables": len(document["environment"]), "fingerprints": document["fingerprints"]}))
    return 0


def _verify(args) -> int:
    result = env_delta.verify(args.container, args.dir)
    print(json.dumps(result))
    return 0 if result["verified"] else 1


def _watch(args) -> int:
    root = Path(args.out_root)
    root.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    previous: str | None = args.previous
    while time.monotonic() < (args.deadline or float("inf")):
        listing = env_delta.default_runner(["docker", "exec", args.container, "ls", "-1", args.control])
        names = {line.strip() for line in listing.stdout.decode(errors="replace").splitlines() if line.strip()}
        for name in sorted(names - seen):
            probe = env_delta.default_runner(
                ["docker", "exec", args.container, "test", "-f", f"{args.control}/{name}/request.json"])
            if probe.returncode != 0:
                continue
            seen.add(name)
            target = root / name
            manifest = env_delta.capture(args.container, target, previous_dir=previous)
            env_delta.record_environment(args.container, target)
            previous = str(target)
            print(json.dumps({"checkpoint": name, **manifest["counts"]}), flush=True)
        time.sleep(args.interval)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture")
    capture.add_argument("--container", required=True)
    capture.add_argument("--out", required=True)
    capture.add_argument("--previous")
    capture.add_argument("--no-environment", action="store_true")
    capture.set_defaults(handler=_capture)

    restore = sub.add_parser("restore")
    restore.add_argument("--container", required=True)
    restore.add_argument("--deltas", nargs="+", required=True)
    restore.set_defaults(handler=_restore)

    env = sub.add_parser("env")
    env.add_argument("--container", required=True)
    env.add_argument("--out", required=True)
    env.set_defaults(handler=_env)

    verify = sub.add_parser("verify")
    verify.add_argument("--container", required=True)
    verify.add_argument("--dir", required=True)
    verify.set_defaults(handler=_verify)

    watch = sub.add_parser("watch")
    watch.add_argument("--container", required=True)
    watch.add_argument("--control", default="/control")
    watch.add_argument("--out-root", required=True)
    watch.add_argument("--previous")
    watch.add_argument("--interval", type=float, default=2.0)
    watch.add_argument("--deadline", type=float, default=0.0)
    watch.set_defaults(handler=_watch)

    args = parser.parse_args(argv)
    return args.handler(args)


if __name__ == "__main__":
    raise SystemExit(main())
