"""SPEC-021 Tasks 2.2-2.4 — built-in self-modification tools.

Verifies ``reload``, ``create_tool``, ``create_skill``, ``update_tool``,
``update_skill``. Each operates on the workspace ``.capabilities/``
sub-tree and either delegates to :class:`CapabilityLoader` (reload)
or writes/validates source files.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.builtins.capabilities import _runtime
from arcagent.core.capability_loader import CapabilityLoader
from arcagent.core.capability_registry import CapabilityRegistry


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def configured(tmp_path: Path) -> Path:
    """Workspace + loader configured; capabilities/ subdir present."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".capabilities").mkdir()
    reg = CapabilityRegistry()
    loader = CapabilityLoader(
        scan_roots=[("workspace", workspace / ".capabilities")],
        registry=reg,
    )
    _runtime.configure(workspace=workspace, loader=loader)
    return workspace


@pytest.mark.asyncio
class TestCreateTool:
    async def test_create_persists_file(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool

        source = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(description='greet', version='1.0.0')\n"
            "async def hello() -> str:\n"
            "    return 'hi'\n"
        )
        result = await create_tool(name="hello", source=source)
        assert "Created tool 'hello'" in result
        assert (configured / ".capabilities" / "hello.py").exists()

    async def test_create_rejects_existing(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool

        (configured / ".capabilities" / "exists.py").write_text("# stub\n")
        result = await create_tool(name="exists", source="# stub\n")
        assert "already exists" in result

    async def test_create_rejects_bad_ast(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool

        result = await create_tool(name="bad", source="import os\n")
        assert "AST validation rejected" in result

    async def test_create_rejects_bad_name(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool

        result = await create_tool(name="not-a-name", source="# x")
        assert "not a valid Python identifier" in result


@pytest.mark.asyncio
class TestCreateSkill:
    async def test_create_skill_scaffolds(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.create_skill import create_skill

        result = await create_skill(
            name="my-skill",
            description="does X",
            triggers=["do x"],
            tools=["read", "write"],
        )
        assert "Created skill 'my-skill'" in result
        folder = configured / ".capabilities/skills" / "my-skill"
        assert folder.is_dir()
        assert (folder / "SKILL.md").exists()
        assert (folder / "references").is_dir()
        assert (folder / "scripts").is_dir()
        assert (folder / "templates").is_dir()
        assert (folder / "assets").is_dir()
        body = (folder / "SKILL.md").read_text()
        assert "name: my-skill" in body
        for header in [
            "## Resources",
            "## Contract",
            "## Knowledge",
            "## Steps",
            "## Anti Patterns",
            "## Examples",
            "## Validation",
        ]:
            assert header in body

    async def test_create_skill_rejects_existing(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.create_skill import create_skill

        (configured / ".capabilities/skills/dup").mkdir(parents=True)
        result = await create_skill(
            name="dup",
            description="x",
            triggers=[],
            tools=[],
        )
        assert "already exists" in result


@pytest.mark.asyncio
class TestUpdateTool:
    async def test_update_bumps_patch(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.update_tool import update_tool

        original = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(description='x', version=\"1.0.0\")\n"
            "async def fn() -> str:\n    return 'a'\n"
        )
        (configured / ".capabilities" / "fn.py").write_text(original)
        new_source = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(description='x', version=\"1.0.1\")\n"
            "async def fn() -> str:\n    return 'b'\n"
        )
        result = await update_tool(name="fn", new_source=new_source, version_bump="patch")
        assert "1.0.0 → 1.0.1" in result
        assert "return 'b'" in (configured / ".capabilities" / "fn.py").read_text()

    async def test_update_rejects_version_mismatch(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.update_tool import update_tool

        original = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(description='x', version=\"1.0.0\")\n"
            "async def fn() -> str:\n    return 'a'\n"
        )
        (configured / ".capabilities" / "fn.py").write_text(original)
        new_source = original  # same — version not bumped
        result = await update_tool(name="fn", new_source=new_source, version_bump="patch")
        assert 'must declare version="1.0.1"' in result


@pytest.mark.asyncio
class TestUpdateSkill:
    async def test_update_bumps_minor(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.update_skill import update_skill

        skill_md = configured / ".capabilities/skills" / "s" / "SKILL.md"
        skill_md.parent.mkdir(parents=True)
        skill_md.write_text(
            "---\n"
            "name: s\n"
            "version: 1.2.3\n"
            "description: x\n"
            "triggers: [a]\n"
            "tools: [read]\n"
            "---\n"
            "\noriginal body\n"
        )
        result = await update_skill(name="s", new_body="updated body", version_bump="minor")
        assert "1.2.3 → 1.3.0" in result
        assert "updated body" in skill_md.read_text()


@pytest.mark.asyncio
class TestReload:
    async def test_reload_returns_diff(self, configured: Path) -> None:
        from arcagent.builtins.capabilities.reload import reload

        result = await reload()
        assert "reload:" in result
