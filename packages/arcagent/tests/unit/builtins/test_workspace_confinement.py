"""Workspace confinement — file and self-modification tools cannot escape.

Live incident (task #20): an agent used its own ``write`` tool to install
files into a SIBLING agent's workspace (``team/coder_agent/workspace/...``).
These tests prove the single choke point — ``_runtime.resolve_workspace_path``,
which binds :func:`arcagent.tools._validation.resolve_workspace_path` to the
agent's runtime state — denies every escape attempt (cross-agent relative
paths, symlink escapes, ``../../`` trickery) for both the plain file tools
(read/write/edit/ls/find/grep) and the self-modification tools
(create_tool/update_tool/create_skill/update_skill), audits every denial
exactly once, and still allows legitimate in-workspace operations.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from arcagent.builtins.capabilities import _runtime
from arcagent.core.errors import ToolError


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def audit_events() -> list[tuple[str, dict[str, Any]]]:
    return []


@pytest.fixture
def team(tmp_path: Path, audit_events: list[tuple[str, dict[str, Any]]]) -> dict[str, Path]:
    """Two sibling agents sharing a team root, mirroring the live incident."""
    josh_workspace = tmp_path / "team" / "josh_agent" / "workspace"
    coder_workspace = tmp_path / "team" / "coder_agent" / "workspace"
    josh_workspace.mkdir(parents=True)
    coder_workspace.mkdir(parents=True)

    def sink(event_type: str, details: dict[str, Any]) -> None:
        audit_events.append((event_type, details))

    _runtime.configure(workspace=josh_workspace, audit_sink=sink)
    return {"josh": josh_workspace, "coder": coder_workspace}


@pytest.mark.asyncio
class TestFileToolsConfinement:
    """read/write/edit/ls/find/grep must stay inside the agent's own workspace."""

    async def test_write_into_sibling_agent_denied(
        self, team: dict[str, Path], audit_events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        from arcagent.builtins.capabilities.write import write

        with pytest.raises(ToolError) as exc_info:
            await write(
                file_path="../../coder_agent/workspace/backdoor.py",
                content="evil",
            )
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"
        assert not (team["coder"] / "backdoor.py").exists()
        assert audit_events, "denial must be audited"
        event_type, details = audit_events[-1]
        assert event_type == "tool.workspace_path.denied"
        assert details["tool"] == "write"

    async def test_write_via_symlink_escape_denied(
        self, team: dict[str, Path], audit_events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        from arcagent.builtins.capabilities.write import write

        link = team["josh"] / "escape"
        link.symlink_to(team["coder"])
        with pytest.raises(ToolError) as exc_info:
            await write(file_path="escape/backdoor.py", content="evil")
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"
        assert not (team["coder"] / "backdoor.py").exists()
        assert audit_events
        assert audit_events[-1][0] == "tool.workspace_path.denied"

    async def test_relative_path_trickery_denied(self, team: dict[str, Path]) -> None:
        from arcagent.builtins.capabilities.edit import edit

        (team["josh"] / "sub").mkdir()
        with pytest.raises(ToolError) as exc_info:
            await edit(
                file_path="sub/../../coder_agent/workspace/x.py",
                old_string="a",
                new_string="b",
            )
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"

    async def test_legitimate_in_workspace_write_allowed(
        self, team: dict[str, Path], audit_events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        from arcagent.builtins.capabilities.write import write

        result = await write(file_path="notes.txt", content="hello")
        assert "Written" in result
        assert (team["josh"] / "notes.txt").read_text() == "hello"
        assert audit_events == []

    async def test_ls_into_sibling_agent_denied(self, team: dict[str, Path]) -> None:
        from arcagent.builtins.capabilities.ls import ls

        with pytest.raises(ToolError) as exc_info:
            await ls(path="../../coder_agent/workspace")
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"

    async def test_grep_into_sibling_agent_denied(self, team: dict[str, Path]) -> None:
        from arcagent.builtins.capabilities.grep import grep

        with pytest.raises(ToolError) as exc_info:
            await grep(pattern="secret", path="../../coder_agent/workspace")
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"

    async def test_find_into_sibling_agent_denied(self, team: dict[str, Path]) -> None:
        from arcagent.builtins.capabilities.find import find

        with pytest.raises(ToolError) as exc_info:
            await find(pattern="*.py", path="../../coder_agent/workspace")
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"

    async def test_read_into_sibling_agent_denied(self, team: dict[str, Path]) -> None:
        from arcagent.builtins.capabilities.read import read

        (team["coder"] / "secret.txt").write_text("shh")
        with pytest.raises(ToolError) as exc_info:
            await read(file_path="../../coder_agent/workspace/secret.txt")
        assert exc_info.value.code == "TOOL_PATH_OUTSIDE_WORKSPACE"


@pytest.mark.asyncio
class TestSelfModToolsConfinement:
    """create_tool/update_tool/create_skill/update_skill stay inside workspace."""

    async def test_create_tool_via_symlink_ancestor_denied(
        self, team: dict[str, Path], audit_events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool

        (team["josh"] / "capabilities").symlink_to(team["coder"])
        with pytest.raises(ToolError) as exc_info:
            await create_tool(name="backdoor", source="# x\n")
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"
        assert not (team["coder"] / "backdoor.py").exists()
        assert audit_events
        assert audit_events[-1][0] == "tool.workspace_path.denied"
        assert audit_events[-1][1]["tool"] == "create_tool"

    async def test_create_skill_via_symlink_ancestor_denied(
        self, team: dict[str, Path], audit_events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        from arcagent.builtins.capabilities.create_skill import create_skill

        (team["josh"] / "capabilities").symlink_to(team["coder"])
        with pytest.raises(ToolError) as exc_info:
            await create_skill(name="backdoor", description="x", triggers=[], tools=[])
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"
        assert not (team["coder"] / "skills" / "backdoor").exists()
        assert audit_events

    async def test_update_tool_rejects_traversal_name(self, team: dict[str, Path]) -> None:
        from arcagent.builtins.capabilities.update_tool import update_tool

        result = await update_tool(
            name="../../coder_agent/workspace/capabilities/existing",
            new_source="# x\n",
            version_bump="patch",
        )
        assert "not a valid Python identifier" in result

    async def test_update_tool_via_symlink_ancestor_denied(
        self, team: dict[str, Path], audit_events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        from arcagent.builtins.capabilities.update_tool import update_tool

        # Pre-existing capability, then a sibling-workspace file is planted at
        # the *same relative path* the symlinked ancestor would resolve to.
        (team["coder"] / "capabilities").mkdir()
        original = (
            "from arcagent.tools._decorator import tool\n"
            "@tool(description='x', version=\"1.0.0\")\n"
            "async def fn() -> str:\n    return 'a'\n"
        )
        (team["coder"] / "capabilities" / "fn.py").write_text(original)
        (team["josh"] / "capabilities").symlink_to(team["coder"] / "capabilities")

        new_source = original.replace("1.0.0", "1.0.1")
        with pytest.raises(ToolError) as exc_info:
            await update_tool(name="fn", new_source=new_source, version_bump="patch")
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"
        assert audit_events
        assert audit_events[-1][1]["tool"] == "update_tool"

    async def test_update_skill_rejects_traversal_name(self, team: dict[str, Path]) -> None:
        from arcagent.builtins.capabilities.update_skill import update_skill

        result = await update_skill(
            name="../../coder_agent/workspace/capabilities/skills/existing",
            new_body="evil",
            version_bump="patch",
        )
        assert "must be alphanumeric" in result

    async def test_update_skill_via_symlink_ancestor_denied(
        self, team: dict[str, Path], audit_events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        from arcagent.builtins.capabilities.update_skill import update_skill

        skill_dir = team["coder"] / "capabilities" / "skills" / "s"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: s\nversion: 1.0.0\ndescription: x\ntriggers: [a]\ntools: [read]\n---\n"
            "\noriginal body\n"
        )
        (team["josh"] / "capabilities").symlink_to(team["coder"] / "capabilities")

        with pytest.raises(ToolError) as exc_info:
            await update_skill(name="s", new_body="evil body", version_bump="patch")
        assert exc_info.value.code == "TOOL_SYMLINK_DENIED"
        assert audit_events

    async def test_create_tool_legitimate_write_still_allowed(
        self, team: dict[str, Path], audit_events: list[tuple[str, dict[str, Any]]]
    ) -> None:
        from arcagent.builtins.capabilities.create_tool import create_tool

        result = await create_tool(name="hello", source="# x\n")
        assert "Created tool 'hello'" in result
        assert (team["josh"] / "capabilities" / "hello.py").exists()
        assert audit_events == []
