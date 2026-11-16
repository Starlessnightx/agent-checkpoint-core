from __future__ import annotations

from typing import Any
import fnmatch
import re

from harness.executor import Command
from .artifacts import bounded_output
from .base import ToolContext, ToolResult
from .filesystem import resolve_workspace


def search_text(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    query = str(args.get("query", ""))
    if not query:
        return ToolResult(False, "query must be non-empty",
                          failure_kind="invalid_input")
    try:
        root = resolve_workspace(context, str(args.get("path", ".")))
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    executor = context.executor
    assert executor is not None
    if not executor.which("rg"):
        return _bounded_search(context, root, args, query)
    command = ["rg", "--line-number", "--color", "never", "--hidden",
               "--glob", "!.git/**", "--glob", "!node_modules/**"]
    if args.get("fixed_strings", True):
        command.append("--fixed-strings")
    glob = str(args.get("glob", "")).strip()
    if glob:
        command.extend(["--glob", glob])
    command.extend(["--", query, str(root)])
    result = executor.exec(Command.argv(command, context.workspace, 30))
    output = result.stdout
    bounded, artifacts = bounded_output(context, "search", output)
    if result.return_code == 1:
        return ToolResult(True, "no matches", stdout="[no matches]")
    if result.return_code != 0:
        return ToolResult(
            False,
            "search failed",
            stdout=bounded,
            stderr=result.stderr,
            exit_code=result.return_code,
            artifacts=artifacts,
            failure_kind="command_failed",
        )
    match_count = len(output.splitlines())
    return ToolResult(
        True,
        f"found {match_count} matching lines",
        stdout=bounded,
        artifacts=artifacts,
    )


def _bounded_search(context, root, args, query):
    """Dependency-free text fallback, with explicit coverage limits."""
    executor = context.executor
    fixed = args.get("fixed_strings", True)
    try:
        pattern = None if fixed else re.compile(query)
    except re.error as exc:
        return ToolResult(False, str(exc), failure_kind="invalid_input")
    paths = [root] if executor.is_file(root) else executor.walk(root)
    lines = []; scanned = 0; skipped = 0; size_total = 0; limited = False
    glob = str(args.get("glob", ""))
    for path in paths:
        if scanned >= 2000 or size_total >= 8_000_000 or len(lines) >= 2000:
            limited = True; break
        if any(p in {".git", "node_modules", ".harness"} for p in path.relative_to(root.parent if root == path else root).parts):
            continue
        try:
            path = resolve_workspace(context, str(path))
            if not executor.is_file(path): continue
            if glob and not (fnmatch.fnmatch(path.name, glob) or fnmatch.fnmatch(context.relative(path), glob)):
                continue
            size = executor.size(path)
            if size > 2_000_000 or size_total + size > 8_000_000:
                skipped += 1; continue
            raw = executor.read_file(path).content
            scanned += 1; size_total += len(raw)
            if b"\0" in raw[:8192]: continue
            text = raw.decode("utf-8")
        except (OSError, PermissionError, UnicodeError):
            skipped += 1; continue
        for number, line in enumerate(text.splitlines(), 1):
            matches = query in line if fixed else bool(pattern.search(line))
            if matches:
                lines.append(f"{context.relative(path)}:{number}:{line}")
                if len(lines) >= 2000:
                    limited = True; break
    output, artifacts = bounded_output(context, "search-fallback", "\n".join(lines))
    return ToolResult(True, f"bounded Python search: {len(lines)} matches; {scanned} files scanned, {skipped} skipped",
                      stdout=output or "[no matches in scanned files]", artifacts=artifacts,
                      structured={"search_backend": "python", "coverage_limited": limited or bool(skipped),
                                  "files_scanned": scanned, "files_skipped": skipped})
