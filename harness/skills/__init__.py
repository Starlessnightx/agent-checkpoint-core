"""Discoverable, progressively loaded harness skills."""

from .base import SkillSpec
from .registry import SkillRegistry

__all__ = ["SkillRegistry", "SkillSpec"]
