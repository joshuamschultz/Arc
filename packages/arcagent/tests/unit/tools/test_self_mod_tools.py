"""SPEC-017 Phase 7 Tasks 7.12-7.15 — self-modification tools.

Six tools for agents to edit their own surface:

  * ``create_skill(name, markdown_body)`` — all tiers
  * ``improve_skill(name, new_markdown_body)`` — all tiers
  * ``create_tool(name, python_source)`` — federal DENIED
  * ``create_extension(name, python_source, module_yaml)`` — federal DENIED
  * ``list_artifacts(kind)`` — all tiers
  * ``reload_artifacts()`` — all tiers
"""

from __future__ import annotations

from pathlib import Path

import pytest


class TestCreateSkill:
    async def test_writes_skill_file(self, tmp_path: Path) -> None:
        from arcagent.tools.skill_tools import make_create_skill_tool

        tool = make_create_skill_tool(skills_dir=tmp_path, audit_sink=None)
        await tool.execute(name="my-skill", markdown_body="# My Skill\nsteps here")

        skill_file = tmp_path / "my-skill.md"
        assert skill_file.exists()
        assert "My Skill" in skill_file.read_text()

    async def test_rejects_invalid_name(self, tmp_path: Path) -> None:
        from arcagent.tools.skill_tools import make_create_skill_tool

        tool = make_create_skill_tool(skills_dir=tmp_path)
        with pytest.raises(ValueError):
            await tool.execute(name="../escape", markdown_body="x")

    async def test_audits_creation(self, tmp_path: Path) -> None:
        from arcagent.tools.skill_tools import make_create_skill_tool

        events: list[tuple[str, dict[str, object]]] = []
        tool = make_create_skill_tool(
            skills_dir=tmp_path,
            audit_sink=lambda e, d: events.append((e, d)),
        )
        await tool.execute(name="audit-me", markdown_body="# body")
        assert any(e[0] == "self_mod.skill_created" for e in events)
        assert events[0][1]["name"] == "audit-me"


class TestImproveSkill:
    async def test_replaces_existing_body(self, tmp_path: Path) -> None:
        from arcagent.tools.skill_tools import (
            make_create_skill_tool,
            make_improve_skill_tool,
        )

        create = make_create_skill_tool(skills_dir=tmp_path)
        await create.execute(name="s1", markdown_body="# v1")

        improve = make_improve_skill_tool(skills_dir=tmp_path)
        await improve.execute(name="s1", new_markdown_body="# v2")

        skill_file = tmp_path / "s1.md"
        assert "v2" in skill_file.read_text()
        assert "v1" not in skill_file.read_text()

    async def test_rejects_missing_skill(self, tmp_path: Path) -> None:
        from arcagent.core.errors import ToolError
        from arcagent.tools.skill_tools import make_improve_skill_tool

        improve = make_improve_skill_tool(skills_dir=tmp_path)
        with pytest.raises(ToolError):
            await improve.execute(name="ghost", new_markdown_body="# x")


class TestCreateToolTool:
    """SPEC-017 R-050 — federal DENIED, enterprise approval, personal allowed."""

    async def test_personal_tier_creates_tool(self, tmp_path: Path) -> None:
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools.tool_tools import make_create_tool_tool

        loader = DynamicToolLoader()
        tool = make_create_tool_tool(loader=loader, tier="personal")

        src = (
            "from arcagent.tools._decorator import tool\n"
            "\n"
            "@tool(description='add', classification='read_only')\n"
            "async def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        result = await tool.execute(name="add", python_source=src)
        assert "add" in result
        assert loader.get("add") is not None

    async def test_federal_tier_denies(self) -> None:
        from arcagent.core.errors import ToolError
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools.tool_tools import make_create_tool_tool

        loader = DynamicToolLoader()
        tool = make_create_tool_tool(loader=loader, tier="federal")

        src = (
            "from arcagent.tools._decorator import tool\n"
            "\n"
            "@tool(description='add', classification='read_only')\n"
            "async def add(a: int, b: int) -> int:\n"
            "    return a + b\n"
        )
        with pytest.raises(ToolError) as exc:
            await tool.execute(name="add", python_source=src)
        assert "federal" in str(exc.value).lower()
        assert loader.get("add") is None


class TestListArtifacts:
    async def test_lists_loaded_tools(self, tmp_path: Path) -> None:
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools.tool_tools import make_list_artifacts_tool

        loader = DynamicToolLoader()
        loader.load(
            (
                "from arcagent.tools._decorator import tool\n"
                "\n"
                "@tool(description='x')\n"
                "async def alpha() -> None:\n"
                "    return None\n"
            ),
            name="alpha",
        )
        tool = make_list_artifacts_tool(loader=loader, skills_dir=tmp_path)
        result = await tool.execute(kind="tool")
        assert "alpha" in result

    async def test_lists_skills(self, tmp_path: Path) -> None:
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools.skill_tools import make_create_skill_tool
        from arcagent.tools.tool_tools import make_list_artifacts_tool

        loader = DynamicToolLoader()
        create = make_create_skill_tool(skills_dir=tmp_path)
        await create.execute(name="my-skill", markdown_body="# body")

        tool = make_list_artifacts_tool(loader=loader, skills_dir=tmp_path)
        result = await tool.execute(kind="skill")
        assert "my-skill" in result


class TestReloadArtifacts:
    async def test_reload_is_read_only_refresh(self, tmp_path: Path) -> None:
        from arcagent.tools._dynamic_loader import DynamicToolLoader
        from arcagent.tools.tool_tools import make_reload_artifacts_tool

        loader = DynamicToolLoader()
        tool = make_reload_artifacts_tool(loader=loader, skills_dir=tmp_path)
        result = await tool.execute()
        assert "reloaded" in result.lower() or "refreshed" in result.lower()
