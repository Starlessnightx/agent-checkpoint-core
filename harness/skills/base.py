from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SkillSpec:
    """Python declaration for a reusable agent workflow.

    Metadata is intentionally Python-only. The instruction body and optional
    resources remain separate so they are loaded only after the model selects
    the skill.
    """

    name: str
    description: str
    instruction_path: str
    tools: tuple[str, ...]
    triggers: tuple[str, ...]
    completion_criteria: tuple[str, ...]
    tool_provider: str
    scripts: tuple[str, ...] = ()
    references: tuple[str, ...] = ()
    tests: tuple[str, ...] = ()
    version: int = 1

    def validate(self, directory: Path) -> None:
        if not self.name or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in self.name):
            raise ValueError(f"invalid skill name: {self.name!r}")
        if not self.description.strip() or not self.tools or not self.triggers:
            raise ValueError(f"skill {self.name} lacks actionable metadata")
        if not self.completion_criteria:
            raise ValueError(f"skill {self.name} lacks completion criteria")
        if not self.tests:
            raise ValueError(f"skill {self.name} lacks a declared contract test")
        instruction = _declared_file(directory, self.instruction_path)
        frontmatter = read_skill_frontmatter(instruction)
        if frontmatter["name"] != self.name:
            raise ValueError(
                f"skill frontmatter name {frontmatter['name']!r} does not match Python declaration {self.name!r}"
            )
        if frontmatter["description"] != self.description:
            raise ValueError(
                f"skill {self.name} frontmatter description does not match Python declaration"
            )
        _declared_file(directory, self.tool_provider)
        for relative in self.scripts + self.references + self.tests:
            _declared_file(directory, relative)


def _declared_file(directory: Path, relative: str) -> Path:
    root = directory.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"skill resource escapes its directory: {relative}") from exc
    if not path.is_file():
        raise ValueError(f"declared skill resource does not exist: {relative}")
    return path


def read_skill_frontmatter(path: Path) -> dict[str, str]:
    """Read the required Agent Skills frontmatter without adding a YAML runtime dependency."""
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError(f"{path} must start with Agent Skills YAML frontmatter")
    try:
        closing = next(
            index for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise ValueError(f"{path} has unterminated Agent Skills frontmatter") from exc
    metadata: dict[str, str] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.partition(":")
        if not separator or not key.strip() or not value.strip():
            raise ValueError(f"{path} has unsupported frontmatter line: {line!r}")
        key = key.strip()
        if key in metadata:
            raise ValueError(f"{path} repeats frontmatter field: {key}")
        metadata[key] = value.strip().strip('"').strip("'")
    for required in ("name", "description"):
        if not metadata.get(required):
            raise ValueError(f"{path} frontmatter is missing {required}")
    return metadata
