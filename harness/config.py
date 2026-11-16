from __future__ import annotations

from pathlib import Path

from .spec import HarnessSpec, build_spec


class HarnessConfig:
    """Compatibility name for callers migrating to the Python HarnessSpec."""

    def __init__(self, root: Path, spec: HarnessSpec):
        self.root = root.resolve()
        self.spec = spec
        self.workspace = spec.workspace

    @classmethod
    def load(cls, root: Path) -> "HarnessConfig":
        root = root.resolve()
        return cls(root, build_spec(root))
