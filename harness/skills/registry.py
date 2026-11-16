from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import SkillSpec, _declared_file


@dataclass(frozen=True)
class LoadedSkill:
    spec: SkillSpec
    directory: Path

    def instructions(self) -> str:
        return _declared_file(
            self.directory, self.spec.instruction_path
        ).read_text(encoding="utf-8")


class SkillRegistry:
    def __init__(self, root: Path | None = None):
        self.root = (root or Path(__file__).resolve().parent).resolve()
        self._skills = self._discover()

    def _discover(self) -> dict[str, LoadedSkill]:
        result: dict[str, LoadedSkill] = {}
        for declaration in sorted(self.root.glob("*/skill.py")):
            module_name = "harness_skill_" + declaration.parent.name
            module_spec = importlib.util.spec_from_file_location(module_name, declaration)
            if module_spec is None or module_spec.loader is None:
                raise ValueError(f"cannot load skill declaration: {declaration}")
            module = importlib.util.module_from_spec(module_spec)
            module_spec.loader.exec_module(module)
            skill = getattr(module, "SKILL", None)
            if not isinstance(skill, SkillSpec):
                raise ValueError(f"{declaration} must declare SKILL: SkillSpec")
            skill.validate(declaration.parent)
            if skill.name in result:
                raise ValueError(f"duplicate skill: {skill.name}")
            result[skill.name] = LoadedSkill(skill, declaration.parent.resolve())
        return result

    def names(self) -> tuple[str, ...]:
        return tuple(self._skills)

    def summaries(self) -> list[dict[str, Any]]:
        return [{
            "name": loaded.spec.name,
            "description": loaded.spec.description,
            "triggers": list(loaded.spec.triggers),
            "tools": list(loaded.spec.tools),
            "tool_provider": loaded.spec.tool_provider,
            "scripts": list(loaded.spec.scripts),
            "references": list(loaded.spec.references),
            "tests": list(loaded.spec.tests),
            "version": loaded.spec.version,
        } for loaded in self._skills.values()]

    def index_prompt(self, names: tuple[str, ...] | None = None, *, independent_tools: bool = False) -> str:
        selected = [
            item for name, item in self._skills.items()
            if names is None or name in names
        ]
        if not selected:
            return "No optional skills are installed."
        lines = [
            ("Optional workflow guides (read when relevant; tools are independently configured, use tool_search for discovery):"
             if independent_tools else "Optional capability index (load a skill to install its runtime tools):")
        ]
        lines.extend(
            f"- {item.spec.name}: {item.spec.description}"
            + (f" (instructions: {item.directory / item.spec.instruction_path}; load with load_skill)"
               if independent_tools else "")
            for item in selected
        )
        return "\n".join(lines)

    def get(self, name: str) -> LoadedSkill:
        try:
            return self._skills[name]
        except KeyError as exc:
            raise ValueError(f"unknown skill: {name}") from exc

    def load(self, name: str) -> dict[str, Any]:
        loaded = self.get(name)
        spec = loaded.spec
        return {
            "name": spec.name,
            "version": spec.version,
            "instructions": loaded.instructions(),
            "tools": list(spec.tools),
            "tool_provider": spec.tool_provider,
            "scripts": list(spec.scripts),
            "references": list(spec.references),
            "tests": list(spec.tests),
            "completion_criteria": list(spec.completion_criteria),
        }

    def tool_specs(self, name: str) -> tuple[Any, ...]:
        """Load the executable ToolSpecs bundled by one capability skill."""
        from harness.tools.base import ToolSpec

        loaded = self.get(name)
        provider = _declared_file(loaded.directory, loaded.spec.tool_provider)
        module_name = "harness_skill_tools_" + loaded.spec.name.replace("-", "_")
        module_spec = importlib.util.spec_from_file_location(module_name, provider)
        if module_spec is None or module_spec.loader is None:
            raise ValueError(f"cannot load skill tool provider: {provider}")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        factory = getattr(module, "tool_specs", None)
        if not callable(factory):
            raise ValueError(f"{provider} must define tool_specs()")
        values = tuple(factory())
        if not values or not all(isinstance(value, ToolSpec) for value in values):
            raise ValueError(f"{provider} returned invalid ToolSpecs")
        actual = tuple(value.name for value in values)
        if actual != loaded.spec.tools:
            raise ValueError(
                f"skill {name} declares {loaded.spec.tools} but provides {actual}"
            )
        return values

    def read_reference(self, name: str, reference: str) -> str:
        loaded = self.get(name)
        if reference not in loaded.spec.references:
            raise ValueError(f"reference is not declared by {name}: {reference}")
        return _declared_file(loaded.directory, reference).read_text(encoding="utf-8")

    def run_script(
        self,
        name: str,
        script: str,
        workspace: Path,
        arguments: list[str],
        timeout_seconds: float,
        executor=None,
    ):
        loaded = self.get(name)
        if script not in loaded.spec.scripts:
            raise ValueError(f"script is not declared by {name}: {script}")
        path = _declared_file(loaded.directory, script)
        command = [sys.executable, str(path), str(workspace.resolve()), *arguments]
        if executor is None:
            from harness.executor.factory import create_executor

            executor = create_executor(workspace)
        from harness.executor import Command

        return executor.exec(Command.argv(command, workspace, timeout_seconds))

    def inspect(self) -> str:
        return json.dumps(self.summaries(), ensure_ascii=False, indent=2)
