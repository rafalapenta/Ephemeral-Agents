from __future__ import annotations

import unittest

from pydantic import ValidationError

from src.database.schemas import AgentCreate, AgentToolCreate, ToolCreate, ToolType


class CatalogContractTests(unittest.TestCase):
    def test_agent_create_normalizes_and_validates_contract(self) -> None:
        agent = AgentCreate(
            agent_id="engineering-frontend-developer",
            macro_domain="engineering",
            name="Frontend Developer",
            system_prompt_path="engineering/engineering-frontend-developer.md",
            trigger_hooks=["Modern frontend applications", " UI implementation "],
        )

        self.assertEqual(
            agent.trigger_hooks, ["Modern frontend applications", "UI implementation"]
        )
        self.assertTrue(agent.is_active)
        self.assertIsNone(agent.squad)

    def test_agent_create_rejects_empty_trigger_hooks(self) -> None:
        with self.assertRaises(ValidationError):
            AgentCreate(
                agent_id="broken",
                macro_domain="engineering",
                name="Broken",
                system_prompt_path="broken.md",
                trigger_hooks=[],
            )

    def test_tool_create_enforces_allowlisted_types_and_safe_defaults(self) -> None:
        tool = ToolCreate(
            tool_id="repo-search",
            name="Repository Search",
            tool_type=ToolType.FASTMCP,
        )

        self.assertEqual(tool.connection_config, {})
        self.assertEqual(tool.input_schema, {})
        self.assertEqual(tool.output_schema, {})
        self.assertFalse(tool.read_only)

    def test_agent_tool_contract_requires_nonempty_ids(self) -> None:
        with self.assertRaises(ValidationError):
            AgentToolCreate(agent_id="", tool_id="repo-search")


if __name__ == "__main__":
    unittest.main()
