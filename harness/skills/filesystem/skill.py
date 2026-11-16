from harness.skills.base import SkillSpec

SKILL = SkillSpec(
    name="filesystem",
    description="Discover, read, search, view images, create, and edit workspace files with bounded, workspace-confined tools. Use whenever a task requires inspecting directories, source code or pictures, locating text, reading files, writing artifacts, replacing text, or applying patches.",
    instruction_path="SKILL.md",
    tool_provider="tools.py",
    tools=("list_directory", "glob_files", "search_text", "read_file", "view_image", "read_many_files", "write_file", "replace_text", "apply_patch"),
    triggers=("file", "directory", "source code", "image", "picture", "visual", "search", "read", "write", "edit", "patch"),
    completion_criteria=("Every accessed path stays inside the workspace", "Large reads are paginated or archived", "Writes return an explicit StateDelta"),
    scripts=("scripts/inventory.py",),
    references=("references/tool-contracts.md", "references/safety-and-errors.md"),
    tests=("tests/test_contract.py",),
)
