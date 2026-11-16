from __future__ import annotations

import os
from pathlib import Path

from .base import Executor
from .docker import DockerExecutor
from .local import ContainedExecutor, LocalExecutor


def create_executor(workspace: Path) -> Executor:
    backend = os.getenv("HARNESS_EXECUTOR", "local").strip().lower()
    if backend == "docker":
        return DockerExecutor(
            workspace,
            image=os.getenv("HARNESS_SANDBOX_IMAGE", "python:3.12-alpine"),
        )
    if backend == "local":
        return LocalExecutor(workspace)
    if backend == "contained":
        return ContainedExecutor(workspace)
    raise ValueError(f"unknown HARNESS_EXECUTOR backend: {backend}")


__all__ = ["create_executor"]
