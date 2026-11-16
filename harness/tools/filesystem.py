from __future__ import annotations

import fnmatch
from hashlib import sha256
from pathlib import Path
from typing import Any

from safety.boundaries import ensure_within

from .artifacts import bounded_output
from .base import StateDelta, ToolContext, ToolResult


IGNORED_PARTS = frozenset({
    ".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache",
})


def resolve_workspace(context: ToolContext, supplied: str) -> Path:
    if not supplied:
        raise PermissionError("path must be non-empty")
    value = Path(supplied)
    target = value if value.is_absolute() else context.workspace / value
    return ensure_within(target, context.filesystem_root or context.workspace)


def list_directory(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        root = resolve_workspace(context, str(args.get("path", ".")))
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    executor = context.executor
    assert executor is not None
    if not executor.exists(root):
        return ToolResult(False, f"not found: {args.get('path', '.')}",
                          failure_kind="not_found")
    if not executor.is_dir(root):
        return ToolResult(False, f"not a directory: {args.get('path', '.')}",
                          failure_kind="invalid_input")
    offset = max(0, int(args.get("offset", 0)))
    limit = min(500, max(1, int(args.get("limit", 200))))
    entries = sorted(
        executor.list_directory(root),
        key=lambda value: (not executor.is_dir(value), value.name),
    )
    selected = entries[offset:offset + limit]
    lines = []
    for path in selected:
        kind = "dir" if executor.is_dir(path) else "file"
        try:
            size = executor.size(path)
        except OSError:
            size = -1
        lines.append(f"{kind}\t{size}\t{context.relative(path)}")
    if offset + limit < len(entries):
        lines.append(f"[more entries: next_offset={offset + limit}]")
    return ToolResult(True, f"listed {len(selected)}/{len(entries)} entries",
                      stdout="\n".join(lines) or "[empty directory]")


def glob_files(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    pattern = str(args.get("pattern", ""))
    if not pattern:
        return ToolResult(False, "pattern must be non-empty",
                          failure_kind="invalid_input")
    try:
        root = resolve_workspace(context, str(args.get("path", ".")))
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    limit = min(2000, max(1, int(args.get("limit", 500))))
    matches = []
    executor = context.executor
    assert executor is not None
    for path in executor.walk(root):
        relative_parts = path.relative_to(root).parts
        if any(part in IGNORED_PARTS for part in relative_parts):
            continue
        relative = str(path.relative_to(root))
        if fnmatch.fnmatch(relative, pattern) or fnmatch.fnmatch(path.name, pattern):
            matches.append(context.relative(path))
            if len(matches) >= limit:
                break
    return ToolResult(
        True,
        f"matched {len(matches)} paths" + (
            f" (limited to {limit})" if len(matches) == limit else ""
        ),
        stdout="\n".join(matches) or "[no matches]",
    )


def read_file(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        path = resolve_workspace(context, str(args["path"]))
    except (KeyError, PermissionError) as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    executor = context.executor
    assert executor is not None
    if not executor.is_file(path):
        return ToolResult(False, f"not a file: {args.get('path')}",
                          failure_kind="not_found")
    try:
        raw = executor.read_file(path).content
    except OSError as exc:
        return ToolResult(False, str(exc), failure_kind="io")
    if b"\x00" in raw[:8192]:
        return ToolResult(False, "binary file; use a task-specific inspection command",
                          failure_kind="binary_file")
    text = raw.decode(errors="replace")
    lines = text.splitlines()
    start = max(1, int(args.get("start_line", 1)))
    count = min(2000, max(1, int(args.get("line_count", 400))))
    selected = lines[start - 1:start - 1 + count]
    numbered = "\n".join(
        f"{number:>6} | {line}"
        for number, line in enumerate(selected, start=start)
    )
    if start - 1 + count < len(lines):
        numbered += f"\n[more lines: next_start_line={start + count}]"
    bounded, artifacts = bounded_output(context, "read-file", numbered)
    return ToolResult(
        True,
        f"read lines {start}-{start + len(selected) - 1} of {len(lines)}",
        stdout=bounded,
        artifacts=artifacts,
    )


def write_file(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        path = resolve_workspace(context, str(args["path"]))
        content = str(args["content"])
    except (KeyError, PermissionError) as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    executor = context.executor
    assert executor is not None
    try:
        written = executor.write_file(path, content.encode())
    except OSError as exc:
        return ToolResult(False, str(exc), failure_kind="io")
    new_hash = sha256(content.encode()).hexdigest()[:12]
    delta = StateDelta(
        modified=(context.relative(path),) if written.existed else (),
        created=() if written.existed else (context.relative(path),),
    )
    return ToolResult(
        True,
        (
            f"{'updated' if written.existed else 'created'} {context.relative(path)}; "
            f"bytes={len(content)} sha256={new_hash} "
            f"previous_bytes={written.previous_size}"
        ),
        state_delta=delta,
    )


def replace_text(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        path = resolve_workspace(context, str(args["path"]))
        old = str(args["old"])
        new = str(args["new"])
    except (KeyError, PermissionError) as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    executor = context.executor
    assert executor is not None
    if not executor.is_file(path):
        return ToolResult(False, f"not a file: {args.get('path')}",
                          failure_kind="not_found")
    text = executor.read_file(path).content.decode(errors="replace")
    count = text.count(old)
    if count != 1:
        return ToolResult(
            False,
            f"expected one exact match, found {count}",
            failure_kind="match_count",
        )
    updated = text.replace(old, new, 1)
    return write_file(context, {"path": context.relative(path), "content": updated})
