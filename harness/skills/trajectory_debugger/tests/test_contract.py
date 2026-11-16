import unittest

from harness.skills.registry import SkillRegistry


class TrajectoryDebuggerContractTest(unittest.TestCase):
    def test_declaration_matches_provider(self):
        registry = SkillRegistry()
        declared = registry.get("trajectory-debugger").spec.tools
        provided = tuple(tool.name for tool in registry.tool_specs("trajectory-debugger"))
        self.assertEqual(declared, provided)
