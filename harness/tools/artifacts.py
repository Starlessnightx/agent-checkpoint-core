from __future__ import annotations

from hashlib import sha256

from .base import ToolContext


def store_artifact(
    context: ToolContext,
    category: str,
    content: str,
    suffix: str = ".txt",
) -> str:
    digest = sha256(content.encode(errors="replace")).hexdigest()[:16]
    path = context.artifact_root / category / f"{digest}{suffix}"
    executor = context.executor
    assert executor is not None
    if not executor.exists(path):
        executor.write_file(path, content.encode("utf-8", errors="replace"))
    return str(path.relative_to(context.workspace))


def bounded_output(
    context: ToolContext,
    category: str,
    content: str,
) -> tuple[str, tuple[str, ...]]:
    limit = max(1000, context.output_soft_limit)
    if len(content) <= limit:
        return content, ()
    artifact = store_artifact(context, category, content)
    head_size = limit // 2
    tail_size = limit - head_size
    summary = (
        content[:head_size]
        + f"\n\n[... {len(content) - limit} characters archived ...]\n\n"
        + content[-tail_size:]
    )
    return summary, (artifact,)
