from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(PROJECT_ROOT))

from harness.skills.trajectory_debugger.engine import (  # noqa: E402
    build_index, compare_indexes, failure_report, load_index, query_index,
)


def main() -> None:
    workspace = Path(sys.argv[1]).resolve()
    parser = argparse.ArgumentParser(description="Index and inspect Harness trajectories")
    sub = parser.add_subparsers(dest="command", required=True)
    index = sub.add_parser("index")
    index.add_argument("patterns", nargs="+")
    index.add_argument("--output")
    query = sub.add_parser("query")
    query.add_argument("index")
    query.add_argument("--text", default="")
    query.add_argument("--task", default="")
    query.add_argument("--tool", default="")
    query.add_argument("--failure-kind", default="")
    report = sub.add_parser("report")
    report.add_argument("index")
    report.add_argument("--task", default="")
    compare = sub.add_parser("compare")
    compare.add_argument("left")
    compare.add_argument("right")
    args = parser.parse_args(sys.argv[2:])
    if args.command == "index":
        value = build_index(workspace, args.patterns)
        if args.output:
            target = _inside(workspace, args.output)
            target.write_text(json.dumps(value, indent=2), encoding="utf-8")
        print(json.dumps(value["metrics"], indent=2))
    elif args.command == "query":
        print(json.dumps(query_index(load_index(_inside(workspace, args.index)), text=args.text, task=args.task, tool=args.tool, failure_kind=args.failure_kind), indent=2))
    elif args.command == "report":
        print(failure_report(load_index(_inside(workspace, args.index)), args.task)["markdown"])
    else:
        print(json.dumps(compare_indexes(load_index(_inside(workspace, args.left)), load_index(_inside(workspace, args.right))), indent=2))


def _inside(workspace: Path, relative: str) -> Path:
    target = (workspace / relative).resolve()
    if target != workspace and workspace not in target.parents:
        raise SystemExit(f"path escapes workspace: {relative}")
    return target


if __name__ == "__main__":
    main()
