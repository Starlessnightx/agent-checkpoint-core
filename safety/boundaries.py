from __future__ import annotations

from pathlib import Path


PROTECTED_TOP_LEVEL = frozenset({
    "safety",
    "control_plane",
    "tests",
    "benchmark_runs",
    "evolution_runs",
})

EVOLVABLE_TOP_LEVEL = frozenset({"harness"})
PROTECTED_HARNESS_SEGMENTS = frozenset({"trusted", "tests", "__pycache__"})


def ensure_within(path: Path, root: Path) -> Path:
    """Resolve *path* and reject escapes from *root*."""
    root = root.resolve()
    target = path.resolve()
    if target != root and root not in target.parents:
        raise PermissionError(f"path escapes boundary: {path}")
    return target


def is_protected_source(relative: str | Path) -> bool:
    value = Path(relative)
    return bool(value.parts) and value.parts[0] in PROTECTED_TOP_LEVEL


def validate_candidate_path(relative: str | Path) -> Path:
    """Validate a source path that an evolution candidate wants to change."""
    value = Path(relative)
    if value.is_absolute() or ".." in value.parts or not value.parts:
        raise PermissionError(f"unsafe candidate path: {relative}")
    if is_protected_source(value):
        raise PermissionError(f"candidate path is protected: {relative}")
    if value.parts[0] not in EVOLVABLE_TOP_LEVEL:
        raise PermissionError(f"path is outside evolvable harness: {relative}")
    if value.parts[0] == "harness" and any(
        part in PROTECTED_HARNESS_SEGMENTS for part in value.parts[1:]
    ):
        raise PermissionError(f"candidate path is protected: {relative}")
    return value
