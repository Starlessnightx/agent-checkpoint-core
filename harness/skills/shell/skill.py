from harness.skills.base import SkillSpec

SKILL = SkillSpec(
    name="shell",
    description="Run bounded foreground commands and manage background process lifecycles with structured failure evidence. Use whenever a task requires shell execution, builds, local diagnostics, servers, daemons, watchers, readiness checks, logs, or process cleanup.",
    instruction_path="SKILL.md",
    tool_provider="tools.py",
    tools=("run_command", "start_process", "process_status", "wait_process", "stop_process"),
    triggers=("command", "shell", "build", "execute", "process", "server", "daemon", "log"),
    completion_criteria=("Every foreground command has exit code and failure classification", "Background processes use managed handles", "Large output is archived"),
    scripts=("scripts/probe_tcp.py",),
    references=("references/command-contract.md", "references/process-lifecycle.md", "references/failure-taxonomy.md"),
    tests=("tests/test_contract.py",),
)
