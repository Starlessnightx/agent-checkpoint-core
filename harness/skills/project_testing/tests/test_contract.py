import unittest

from harness.skills.registry import SkillRegistry


class ProjectTestingContractTest(unittest.TestCase):
    def test_declaration_matches_provider(self):
        registry = SkillRegistry()
        declared = registry.get("project-testing").spec.tools
        provided = tuple(tool.name for tool in registry.tool_specs("project-testing"))
        self.assertEqual(declared, provided)
        self.assertEqual(provided, ("inspect_repository", "discover_checks", "run_check"))
