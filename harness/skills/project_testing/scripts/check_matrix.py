from __future__ import annotations

import json
import sys
from pathlib import Path


workspace = Path(sys.argv[1]).resolve()
relative = sys.argv[2] if len(sys.argv) > 2 else "."
root = (workspace / relative).resolve()
if root != workspace and workspace not in root.parents:
    raise SystemExit(f"path escapes workspace: {relative}")

matrix = []
rules = {
    "pyproject.toml": ["python project: inspect [tool.pytest], build-system, and scripts"],
    "package.json": ["javascript project: inspect scripts and lockfile before choosing package manager"],
    "Cargo.toml": ["rust project: inspect workspace packages and features"],
    "go.mod": ["go project: inspect packages, build tags, and Make targets"],
    "Makefile": ["inspect declared test/check/lint/build targets"],
}
for marker, guidance in rules.items():
    path = root / marker
    if path.is_file():
        matrix.append({"marker": str(path.relative_to(workspace)), "guidance": guidance})
print(json.dumps(matrix, indent=2))
