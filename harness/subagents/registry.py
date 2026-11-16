from __future__ import annotations

from pathlib import Path
from dataclasses import replace

from .base import SubagentContextSpec, SubagentSpec


_PROMPTS = Path(__file__).resolve().parent / "prompts"


def default_subagents() -> tuple[SubagentSpec, ...]:
    roles = (
        SubagentSpec(
            name="explorer",
            description="Map relevant repository structure, constraints, entry points, and project-native checks without editing.",
            prompt_path=str(_PROMPTS / "explorer.md"),
            tools=("list_directory", "glob_files", "search_text", "read_file", "view_image", "read_many_files", "inspect_repository", "discover_checks", "list_skills", "load_skill", "read_skill_reference"),
            skills=("filesystem", "project-testing"),
            context=SubagentContextSpec(),
            workspace_modes=("read_only",),
            default_workspace_mode="read_only",
        ),
        SubagentSpec(
            name="debugger",
            description="Analyze a concrete failure, produce falsifiable root-cause hypotheses, and recommend discriminating checks.",
            prompt_path=str(_PROMPTS / "debugger.md"),
            tools=("list_directory", "glob_files", "search_text", "read_file", "view_image", "read_many_files", "inspect_repository", "discover_checks", "build_trajectory_index", "query_trajectory_index", "report_failure_episode", "compare_trajectory_runs", "list_skills", "load_skill", "read_skill_reference", "run_skill_script"),
            skills=("filesystem", "project-testing", "trajectory-debugger"),
            context=SubagentContextSpec(),
            workspace_modes=("read_only",),
            default_workspace_mode="read_only",
        ),
        SubagentSpec(
            name="implementer",
            description="Implement one bounded change in an isolated workspace and return a reviewable patch.",
            prompt_path=str(_PROMPTS / "implementer.md"),
            tools=("list_directory", "glob_files", "search_text", "read_file", "view_image", "read_many_files", "write_file", "replace_text", "apply_patch", "run_command", "start_process", "process_status", "wait_process", "stop_process", "inspect_repository", "discover_checks", "run_check", "inspect_harness_structure", "validate_harness_structure", "analyze_harness_change", "run_harness_contracts", "list_skills", "load_skill", "read_skill_reference", "run_skill_script"),
            skills=("filesystem", "shell", "project-testing"),
            context=SubagentContextSpec(),
            workspace_modes=("patch_only",),
            default_workspace_mode="patch_only",
        ),
        SubagentSpec(
            name="verifier",
            description="Independently inspect and test the current deliverable without trusting implementation claims.",
            prompt_path=str(_PROMPTS / "verifier.md"),
            tools=("list_directory", "glob_files", "search_text", "read_file", "view_image", "read_many_files", "inspect_repository", "discover_checks", "run_check", "build_trajectory_index", "query_trajectory_index", "report_failure_episode", "compare_trajectory_runs", "inspect_harness_structure", "validate_harness_structure", "analyze_harness_change", "run_harness_contracts", "list_skills", "load_skill", "read_skill_reference", "run_skill_script"),
            skills=("filesystem", "project-testing", "trajectory-debugger"),
            context=SubagentContextSpec(),
            workspace_modes=("read_only",),
            default_workspace_mode="read_only",
        ),
    )
    return roles + (replace(
        roles[2], name="researcher",
        description="Test one falsifiable hypothesis with a small reproducible experiment in an isolated workspace; return evidence and limitations.",
        prompt_path=str(_PROMPTS / "researcher.md"),
        skills=("filesystem", "shell", "project-testing"),
        tools=tuple(name for name in roles[2].tools if name not in {
            "inspect_harness_structure", "validate_harness_structure",
            "analyze_harness_change", "run_harness_contracts"}),
    ),)
