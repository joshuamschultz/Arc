"""SPEC-017 R-050 Tasks 7.17-7.18 — tier-aware self-modification.

End-to-end verification that:

  * Federal tier DENIES ``create_tool`` before the loader is consulted.
  * Personal tier ALLOWS ``create_tool``; subsequent registry call is
    governed by the policy pipeline (no separate enforcement path).

These tests run the real tools against real loaders; the tier string
is the only variable.
"""

from __future__ import annotations

from pathlib import Path

import pytest


_SAFE_TOOL_SRC = (
    "from arcagent.tools._decorator import tool\n"
    "\n"
    "@tool(description='add', classification='read_only')\n"
    "async def adder(a: int, b: int) -> int:\n"
    "    return a + b\n"
)


class TestFederalTier:
    async def test_create_tool_denied(self, tmp_path: Path) -> None:
        from arcagent.core.errors import ToolError
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools.tool_tools import make_create_tool_tool

        loader = DynamicToolLoader()
        events: list[tuple[str, dict]] = []
        tool = make_create_tool_tool(
            loader=loader,
            tier="federal",
            audit_sink=lambda e, d: events.append((e, d)),
        )
        with pytest.raises(ToolError) as exc_info:
            await tool.execute(name="adder", python_source=_SAFE_TOOL_SRC)

        # Denial carries the federal-specific code + message
        assert exc_info.value.code == "SELF_MOD_FEDERAL_DENIED"
        # Loader was never consulted
        assert loader.get("adder") is None
        # Audit event recorded
        deny_events = [e for e in events if e[0] == "self_mod.tool_create_denied"]
        assert len(deny_events) == 1
        assert deny_events[0][1]["tier"] == "federal"

    async def test_skill_tools_still_work_in_federal(self, tmp_path: Path) -> None:
        """Federal tier bans dynamic code but MUST allow skill edits
        (skills are declarative markdown, not executable code)."""
        from arcagent.tools.skill_tools import make_create_skill_tool

        skills_dir = tmp_path / "skills"
        create = make_create_skill_tool(skills_dir=skills_dir)
        await create.execute(name="federal-skill", markdown_body="# Allowed in fed tier")
        assert (skills_dir / "federal-skill.md").exists()


class TestPersonalTier:
    async def test_create_tool_succeeds(self, tmp_path: Path) -> None:
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools.tool_tools import make_create_tool_tool

        loader = DynamicToolLoader()
        tool = make_create_tool_tool(loader=loader, tier="personal")
        result = await tool.execute(name="adder", python_source=_SAFE_TOOL_SRC)
        assert "adder" in result

        registered = loader.get("adder")
        assert registered is not None
        assert registered.classification == "read_only"

    async def test_created_tool_is_callable(self, tmp_path: Path) -> None:
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools.tool_tools import make_create_tool_tool

        loader = DynamicToolLoader()
        tool = make_create_tool_tool(loader=loader, tier="personal")
        await tool.execute(name="adder", python_source=_SAFE_TOOL_SRC)

        registered = loader.get("adder")
        result = await registered.execute(a=7, b=35)
        assert result == 42

    async def test_malicious_source_still_rejected_in_personal(self) -> None:
        """Personal tier does not bypass the AST validator."""
        from arcagent.tools._dynamic_loader import (
            ASTValidationError,
            DynamicToolLoader,
        )
        from arcagent.tools.tool_tools import make_create_tool_tool

        loader = DynamicToolLoader()
        tool = make_create_tool_tool(loader=loader, tier="personal")
        malicious_src = "import os\n"
        with pytest.raises(ASTValidationError):
            await tool.execute(name="evil", python_source=malicious_src)
        assert loader.get("evil") is None


class TestEnterpriseTier:
    async def test_enterprise_loads_with_audit(self, tmp_path: Path) -> None:
        """Enterprise tier allows creation but records an audit event
        so the approval workflow can ingest it."""
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools.tool_tools import make_create_tool_tool

        loader = DynamicToolLoader()
        events: list[tuple[str, dict]] = []
        tool = make_create_tool_tool(
            loader=loader,
            tier="enterprise",
            audit_sink=lambda e, d: events.append((e, d)),
        )
        await tool.execute(name="adder", python_source=_SAFE_TOOL_SRC)

        # Loader accepted + audit event contains tier="enterprise"
        assert loader.get("adder") is not None
        created = [e for e in events if e[0] == "self_mod.tool_created"]
        assert len(created) == 1
        assert created[0][1]["tier"] == "enterprise"
