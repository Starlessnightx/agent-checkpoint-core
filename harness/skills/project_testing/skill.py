from harness.skills.base import SkillSpec

SKILL = SkillSpec(
    name="project-testing",
    description="Inspect repository test machinery, discover project-native checks, and execute structured verification. Use whenever a task requires testing, linting, type checking, building, validating an artifact, selecting a verification command, or proving a change after the final write.",
    instruction_path="SKILL.md",
    tool_provider="tools.py",
    tools=("inspect_repository", "discover_checks", "run_check", "git_status", "git_diff", "git_log", "git_show"),
    triggers=("repository", "project", "test", "verify", "lint", "build", "check"),
    completion_criteria=("Checks have repository source evidence", "Verification names the tested target", "Evidence was produced after the final write"),
    scripts=("scripts/check_matrix.py",),
    references=("references/discovery.md", "references/evidence-and-failures.md"),
    tests=("tests/test_contract.py",),
)
