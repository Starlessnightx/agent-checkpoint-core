from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path


workspace = Path(sys.argv[1]).resolve()
roots = sys.argv[2:] or ["."]
excluded = {".git", ".harness", ".venv", "node_modules", "__pycache__"}
records = []
for supplied in roots:
    root = (workspace / supplied).resolve()
    if root != workspace and workspace not in root.parents:
        raise SystemExit(f"path escapes workspace: {supplied}")
    paths = [root] if root.is_file() else root.rglob("*") if root.is_dir() else []
    for path in paths:
        relative = path.relative_to(workspace)
        if not path.is_file() or any(part in excluded for part in relative.parts):
            continue
        data = path.read_bytes()
        records.append({
            "path": str(relative),
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
print(json.dumps(sorted(records, key=lambda item: item["path"]), indent=2))
