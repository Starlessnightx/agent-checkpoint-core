from __future__ import annotations

from typing import Any

from .base import StateDelta, ToolContext, ToolResult
from .filesystem import resolve_workspace
from .patch_format import parse_codex_patch, parse_unified_patch, update_text


def apply_patch(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    patch_text = str(args.get("patch", ""))
    if not patch_text.strip():
        return ToolResult(False, "patch must be non-empty",
                          failure_kind="invalid_input")
    if patch_text.startswith("*** Begin Patch"):
        try:
            edits = parse_codex_patch(patch_text)
        except (ValueError, UnicodeError) as exc:
            return ToolResult(False, str(exc), failure_kind="patch_rejected")
        return _apply_edits(context, edits, "Codex")
    try:
        edits = parse_unified_patch(patch_text)
    except (ValueError, UnicodeError) as exc:
        return ToolResult(False, str(exc), failure_kind="invalid_input")
    return _apply_edits(context, edits, "unified")


def _apply_edits(
    context: ToolContext,
    edits: tuple[Any, ...],
    label: str,
) -> ToolResult:
    executor = context.executor
    assert executor is not None
    originals = {}
    planned = {}
    touched = set()
    try:
        for edit in edits:
            source = resolve_workspace(context, edit.path)
            target = resolve_workspace(context, edit.destination) if edit.destination else source
            for path in {source, target}:
                if path in touched:
                    raise ValueError("duplicate/overlapping patch paths; combine updates")
                touched.add(path)
                if executor.exists(path) and not executor.is_file(path):
                    raise ValueError("patch target is not a regular file")
                originals[path] = executor.read_file(path).content if executor.exists(path) else None
            old = originals[source]
            if edit.operation == "Add":
                if old is not None:
                    raise ValueError("Add File target already exists")
                planned[source] = (
                    "\n".join(line[1:] for line in edit.body)
                    + ("\n" if edit.body else "")
                ).encode()
            elif old is None:
                raise ValueError("Update/Delete File source does not exist")
            elif edit.operation == "Delete":
                planned[source] = None
            else:
                if target != source and originals[target] is not None:
                    raise ValueError("Move destination already exists")
                planned[target] = update_text(old.decode("utf-8"), edit.body).encode("utf-8")
                if target != source:
                    planned[source] = None
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    except (ValueError, UnicodeError, OSError) as exc:
        return ToolResult(False, str(exc), failure_kind="patch_rejected")
    changed = []
    try:
        for path, content in planned.items():
            changed.append(path)
            if content is None:
                executor.remove_file(path)
            else:
                executor.write_file(path, content)
    except Exception as exc:
        rollback_errors = []
        for path in reversed(changed):
            try:
                old = originals[path]
                if old is None:
                    if executor.exists(path):
                        executor.remove_file(path)
                else:
                    executor.write_file(path, old)
            except Exception as rollback:
                rollback_errors.append(str(rollback))
        return ToolResult(
            False, f"patch write failed: {exc}", failure_kind="patch_failed",
            structured={
                "rollback_complete": not rollback_errors,
                "rollback_errors": rollback_errors,
            },
        )
    names = lambda predicate: tuple(context.relative(p) for p in planned if predicate(p))
    return ToolResult(
        True, f"applied {label} patch to {len(planned)} paths",
        state_delta=StateDelta(
            created=names(lambda p: originals[p] is None and planned[p] is not None),
            modified=names(lambda p: originals[p] is not None and planned[p] is not None),
            deleted=names(lambda p: planned[p] is None),
        ),
    )
