from __future__ import annotations

import json

from .registry import SkillRegistry
from harness.tools.artifacts import bounded_output
from harness.tools.base import ToolContext, ToolResult, ToolSpec, object_schema


def skill_tool_specs(registry: SkillRegistry | None = None, *, instructions_only: bool = False,
                     allowed_skills: set[str] | None = None) -> tuple[ToolSpec, ...]:
    skills = registry or SkillRegistry()

    def list_skills(context: ToolContext, arguments: dict) -> ToolResult:
        listing = [
            {"name": item["name"], "description": item["description"]}
            for item in skills.summaries()
            if allowed_skills is None or item["name"] in allowed_skills
        ]
        return ToolResult(
            True, json.dumps(listing, ensure_ascii=False, indent=2)
        )

    def load_skill(context: ToolContext, arguments: dict) -> ToolResult:
        loaded = skills.load(str(arguments["name"]))
        text = loaded["instructions"]
        summary = (
            f"Loaded skill {loaded['name']} v{loaded['version']}.\n"
            f"{'Related tools (not installed or activated by this skill)' if instructions_only else 'Installed tools'}: {', '.join(loaded['tools'])}\n"
            f"Scripts: {', '.join(loaded['scripts']) or 'none'}\n"
            f"References: {', '.join(loaded['references']) or 'none'}\n"
            f"Contract tests: {', '.join(loaded['tests']) or 'none'}\n"
            f"Completion criteria: {'; '.join(loaded['completion_criteria'])}\n\n"
            f"{text}"
        )
        output, artifacts = bounded_output(context, "skills", summary)
        return ToolResult(True, output, artifacts=artifacts)

    def read_skill_reference(context: ToolContext, arguments: dict) -> ToolResult:
        text = skills.read_reference(
            str(arguments["name"]), str(arguments["reference"])
        )
        output, artifacts = bounded_output(context, "skill-references", text)
        return ToolResult(True, output, artifacts=artifacts)

    def run_skill_script(context: ToolContext, arguments: dict) -> ToolResult:
        completed = skills.run_script(
            str(arguments["name"]),
            str(arguments["script"]),
            context.workspace,
            [str(value) for value in arguments.get("arguments", [])],
            float(arguments.get("timeout_seconds", 30)),
            executor=context.executor,
        )
        if completed.timed_out:
            return ToolResult(False, "skill script timed out", failure_kind="timeout", retryable=True)
        combined = completed.stdout
        if completed.stderr:
            combined += "\n[stderr]\n" + completed.stderr
        output, artifacts = bounded_output(context, "skill-scripts", combined)
        return ToolResult(
            completed.return_code == 0,
            f"Skill script finished with exit code {completed.return_code}.",
            stdout=output,
            exit_code=completed.return_code,
            artifacts=artifacts,
            failure_kind=None if completed.return_code == 0 else "skill_script_failed",
        )

    name = {"type": "string", "description": "Exact skill name from list_skills."}
    return (
        ToolSpec("list_skills", "List workflow guides without loading their full instructions." if instructions_only else "List installable capability packages without loading instructions or tool implementations.", object_schema({}), list_skills, "skill:list"),
        ToolSpec("load_skill", "Read one workflow guide; does not install tools, activate schemas or grant permission." if instructions_only else "Load one capability package and install its permitted runtime tools into this agent.", object_schema({"name": name}, ("name",)), load_skill, "skill:load"),
        ToolSpec("read_skill_reference", "Read one reference explicitly declared by a loaded skill.", object_schema({"name": name, "reference": {"type": "string"}}, ("name", "reference")), read_skill_reference, "skill:read"),
        ToolSpec("run_skill_script", "Run a deterministic script explicitly declared by a skill inside the workspace.", object_schema({
            "name": name,
            "script": {"type": "string"},
            "arguments": {"type": "array", "items": {"type": "string"}},
            "timeout_seconds": {"type": "number", "minimum": 0.1, "maximum": 120},
        }, ("name", "script")), run_skill_script, "skill:execute", timeout_seconds=120),
    )
