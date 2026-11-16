from harness.skills.base import SkillSpec


SKILL = SkillSpec(
    name="trajectory-debugger",
    description="Index, query, compare, and diagnose Harness trajectories using source-linked deterministic evidence. Use whenever an agent rollout fails, stalls, repeats actions, finishes without verification, loses context, misuses tools, or changes behavior between baseline and candidate runs.",
    instruction_path="SKILL.md",
    tool_provider="tools.py",
    tools=(
        "build_trajectory_index",
        "query_trajectory_index",
        "report_failure_episode",
        "compare_trajectory_runs",
    ),
    triggers=("trajectory", "trace", "episode", "failure analysis", "tool failure", "compare runs", "debug agent"),
    completion_criteria=(
        "Every diagnosis cites indexed event identifiers",
        "Observed facts are separated from hypotheses",
        "Raw source paths remain recoverable from reports",
    ),
    scripts=("scripts/trajectory_debugger.py",),
    references=(
        "references/index-schema.md",
        "references/failure-analysis.md",
        "references/comparison-method.md",
    ),
    tests=("tests/test_contract.py", "tests/test_engine.py"),
)
