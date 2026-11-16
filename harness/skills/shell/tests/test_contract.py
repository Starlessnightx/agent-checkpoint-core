import unittest

from harness.skills.registry import SkillRegistry


class ShellContractTest(unittest.TestCase):
    def test_declaration_matches_provider(self):
        registry = SkillRegistry()
        declared = registry.get("shell").spec.tools
        provided = tuple(tool.name for tool in registry.tool_specs("shell"))
        self.assertEqual(declared, provided)
        self.assertEqual({"run_command", "start_process", "process_status", "wait_process", "stop_process"}, set(provided))
