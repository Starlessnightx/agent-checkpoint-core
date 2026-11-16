import unittest
from pathlib import Path

from harness.skills.registry import SkillRegistry


class FilesystemContractTest(unittest.TestCase):
    def test_declaration_matches_provider(self):
        registry = SkillRegistry()
        declared = registry.get("filesystem").spec.tools
        provided = tuple(tool.name for tool in registry.tool_specs("filesystem"))
        self.assertEqual(declared, provided)
        self.assertTrue(Path(registry.get("filesystem").directory, "SKILL.md").is_file())
