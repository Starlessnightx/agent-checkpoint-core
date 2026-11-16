from __future__ import annotations

from pathlib import Path
from typing import Any

from harness.tools.artifacts import bounded_output
from harness.tools.base import ToolContext, ToolResult, ToolSpec, object_schema
from harness.tools.filesystem import glob_files, list_directory, read_file, replace_text, resolve_workspace, write_file
from harness.tools.patching import apply_patch
from harness.tools.search import search_text
from harness.media.tool import view_image


def _read_many(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    patterns = [str(value) for value in arguments.get("patterns", [])]
    if not patterns:
        return ToolResult(False, "patterns must not be empty", failure_kind="invalid_input")
    limit = min(int(arguments.get("limit", 40)), 200)
    matched: list[Path] = []
    executor = context.executor
    assert executor is not None
    for pattern in patterns:
        for path in executor.glob(context.workspace, pattern):
            if executor.is_file(path) and path not in matched:
                resolve_workspace(context, str(path.relative_to(context.workspace)))
                matched.append(path)
                if len(matched) >= limit:
                    break
        if len(matched) >= limit:
            break
    sections: list[str] = []
    skipped: list[str] = []
    max_file_chars = int(arguments.get("max_file_chars", 20_000))
    for path in matched:
        relative = str(path.relative_to(context.workspace))
        try:
            text = executor.read_file(path).content.decode("utf-8")
        except (UnicodeDecodeError, OSError):
            skipped.append(relative)
            continue
        suffix = "\n[FILE TRUNCATED: use read_file for pagination]" if len(text) > max_file_chars else ""
        sections.append(f"===== {relative} =====\n{text[:max_file_chars]}{suffix}")
    output, artifacts = bounded_output(context, "read-many", "\n\n".join(sections))
    summary = f"Read {len(sections)} files; skipped {len(skipped)} binary/unreadable files."
    return ToolResult(True, summary, stdout=output, artifacts=artifacts)


def tool_specs() -> tuple[ToolSpec, ...]:
    path = {"type": "string", "description": "Workspace-relative path."}
    return (
        ToolSpec("list_directory", "List exactly one directory level with type and size; paginate broad directories.", object_schema({"path": path, "offset": {"type": "integer", "minimum": 0}, "limit": {"type": "integer", "minimum": 1, "maximum": 500}}), list_directory, "filesystem:read"),
        ToolSpec("glob_files", "Find workspace paths by glob while excluding common caches and dependency trees.", object_schema({"pattern": {"type": "string"}, "path": path, "limit": {"type": "integer", "minimum": 1, "maximum": 2000}}, ("pattern",)), glob_files, "filesystem:read"),
        ToolSpec("search_text", "Search text with ripgrep and return path/line evidence; narrow by path or glob.", object_schema({"query": {"type": "string"}, "path": path, "glob": {"type": "string"}, "fixed_strings": {"type": "boolean"}}, ("query",)), search_text, "filesystem:read"),
        ToolSpec("read_file", "Read UTF-8 text with line numbers and explicit start_line/line_count pagination.", object_schema({"path": path, "start_line": {"type": "integer", "minimum": 1}, "line_count": {"type": "integer", "minimum": 1, "maximum": 2000}}, ("path",)), read_file, "filesystem:read"),
        ToolSpec("view_image", "Inspect a workspace PNG, JPEG, WebP or non-animated GIF using native model vision, not text. Requires image-capable model; no automatic resize. Audit snapshots are not task verification.", object_schema({"path": path, "detail": {"type": "string", "enum": ["auto", "low", "high"]}}, ("path",)), view_image, "filesystem:read"),
        ToolSpec("read_many_files", "Read a bounded set of text files matched by workspace-relative globs, with per-file and aggregate output limits.", object_schema({"patterns": {"type": "array", "items": {"type": "string"}, "minItems": 1}, "limit": {"type": "integer", "minimum": 1, "maximum": 200}, "max_file_chars": {"type": "integer", "minimum": 100, "maximum": 100000}}, ("patterns",)), _read_many, "filesystem:read"),
        ToolSpec("write_file", "Atomically create or replace one UTF-8 file and report created/modified StateDelta.", object_schema({"path": path, "content": {"type": "string"}}, ("path", "content")), write_file, "filesystem:write", mutates_workspace=True),
        ToolSpec("replace_text", "Replace exactly one occurrence; fail on missing or ambiguous text instead of guessing.", object_schema({"path": path, "old": {"type": "string"}, "new": {"type": "string"}}, ("path", "old", "new")), replace_text, "filesystem:write", mutates_workspace=True),
        ToolSpec("apply_patch", "Apply *** Begin Patch / *** Add File: path / *** Update File: path / *** Delete File: path / *** End Patch text, with @@ context hunks and optional *** Move to:. No patch binary needed. Prevalidates all edits; rejects ambiguous context. Traditional unified diff with ---, +++ and @@ hunks is also accepted by the built-in parser.", object_schema({"patch": {"type": "string"}}, ("patch",)), apply_patch, "filesystem:write", mutates_workspace=True),
    )
