from __future__ import annotations

import json
import shlex
from pathlib import Path
from typing import Any

from .base import ToolContext, ToolResult
from harness.executor import Command
from .filesystem import resolve_workspace


MARKERS = {
    "python": ("pyproject.toml", "setup.py", "setup.cfg", "requirements.txt"),
    "javascript": ("package.json",),
    "rust": ("Cargo.toml",),
    "go": ("go.mod",),
    "java": ("pom.xml", "build.gradle", "build.gradle.kts"),
    "c-cpp": ("CMakeLists.txt", "Makefile", "meson.build"),
}


def inspect_repository(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    try:
        root = resolve_workspace(context, str(args.get("path", ".")))
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    executor = context.executor
    assert executor is not None
    found: dict[str, list[str]] = {}
    for ecosystem, names in MARKERS.items():
        values = [name for name in names if executor.exists(root / name)]
        if values:
            found[ecosystem] = values
    checks = discover_check_commands(root, executor)
    facts = {
        "root": context.relative(root),
        "ecosystems": found,
        "candidate_checks": checks,
        "git_repository": executor.exists(root / ".git"),
    }
    return ToolResult(
        True,
        f"detected {len(found)} ecosystems and {len(checks)} candidate checks",
        stdout=json.dumps(facts, indent=2, ensure_ascii=False),
    )


def _git(context: ToolContext, args: dict[str, Any], subcommand: str) -> ToolResult:
    try:
        root = resolve_workspace(context, str(args.get("path", args.get("cwd", "."))))
    except PermissionError as exc:
        return ToolResult(False, str(exc), failure_kind="permission")
    executor = context.executor
    assert executor is not None
    result = executor.exec(Command.argv(["git", *shlex.split(subcommand)], root,
                                        float(args.get("timeout_seconds", 30))))
    ok = result.return_code == 0 and not result.timed_out
    return ToolResult(ok, f"git {subcommand} {'completed' if ok else 'failed'}",
                      stdout=result.stdout, stderr=result.stderr,
                      exit_code=result.return_code,
                      failure_kind=None if ok else ("timeout" if result.timed_out else "git_failed"),
                      retryable=result.timed_out)


def git_status(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    return _git(context, args, "status --short")


def git_diff(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    return _git(context, args, "diff")


def git_log(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    return _git(context, args, "log --oneline -20")


def git_show(context: ToolContext, args: dict[str, Any]) -> ToolResult:
    return _git(context, args, "show --stat")


def discover_check_commands(root: Path, executor=None) -> list[dict[str, str]]:
    if executor is None:
        from harness.executor.factory import create_executor

        executor = create_executor(root)
    values: list[dict[str, str]] = []
    if executor.exists(root / "pyproject.toml") or executor.exists(root / "pytest.ini"):
        values.append({"command": "python3 -m pytest", "source": "python project"})
    if executor.is_dir(root / "tests") and not any(
        item["command"] == "python3 -m pytest" for item in values
    ):
        values.append({
            "command": "python3 -m unittest discover -s tests -v",
            "source": "tests/ directory",
        })
    package = root / "package.json"
    if executor.is_file(package):
        try:
            scripts = json.loads(executor.read_file(package).content.decode()).get("scripts", {})
        except (OSError, json.JSONDecodeError):
            scripts = {}
        for name in ("test", "lint", "build"):
            if name in scripts:
                values.append({
                    "command": f"npm run {name}",
                    "source": f"package.json scripts.{name}",
                })
    if executor.exists(root / "Cargo.toml"):
        values.append({"command": "cargo test", "source": "Cargo.toml"})
    if executor.exists(root / "go.mod"):
        values.append({"command": "go test ./...", "source": "go.mod"})
    if executor.exists(root / "Makefile"):
        values.append({"command": "make test", "source": "Makefile convention"})
    return values
