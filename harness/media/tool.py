from __future__ import annotations

from hashlib import sha256
from typing import Any

from harness.tools.base import ToolContext, ToolResult
from harness.tools.filesystem import resolve_workspace
from .images import ImageAttachment, data_url, image_info


def view_image(context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
    if context.image_input_support is not None and context.image_input_support() is False:
        return ToolResult(False, "The current model/provider does not support native image input. Choose a vision-capable model; do not infer image contents from filenames.", failure_kind="unsupported_image_input")
    detail = arguments.get("detail", "auto")
    if detail not in ("auto", "low", "high"):
        return ToolResult(False, "detail must be auto, low or high", failure_kind="invalid_input")
    try:
        path = resolve_workspace(context, str(arguments.get("path", "")))
        executor = context.executor
        assert executor is not None
        content = executor.read_file(path).content
        mime, width, height = image_info(content)
        digest = sha256(content).hexdigest()
        snapshot = resolve_workspace(context, str(context.artifact_root / "images" / digest))
        # Same Executor-backed audit archive boundary as bounded text reads.
        executor.write_file(snapshot, content)
        attachment = ImageAttachment(context.relative(path), context.relative(snapshot), digest, mime, width, height, str(detail), data_url(content, mime))
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    except FileNotFoundError as exc:
        return ToolResult(False, str(exc), failure_kind="not_found")
    except (OSError, ValueError) as exc:
        return ToolResult(False, str(exc), failure_kind="invalid_image")
    return ToolResult(True, f"Image queued for native visual inspection: {attachment.source} ({width}x{height}, {mime}, detail={detail}). This is not a verification pass.", stdout=f"audit_snapshot={attachment.snapshot}\nsha256={digest}", images=(attachment,), structured={"image": attachment.dictionary(), "audit_only": True})
