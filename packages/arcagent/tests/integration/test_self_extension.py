"""Integration test — self-extension acceptance criteria (PRD §4.1).

Tests the full lifecycle: agent starts → extension written to workspace →
reload() → tool available → tool executes correctly → audit events emitted.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture()
def agent_config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="extension-test-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(
            did="",
            key_dir=str(tmp_path / "keys"),
            vault_path="",
        ),
        telemetry=TelemetryConfig(enabled=True),
    )


def _write_extension(ext_dir: Path, name: str, tool_name: str) -> Path:
    """Write a simple extension file that registers a tool."""
    ext_file = ext_dir / f"{name}.py"
    ext_file.write_text(
        f"""
from arcagent.core.tool_registry import RegisteredTool, ToolTransport


def extension(api):
    async def _execute(**kwargs):
        return "hello from {tool_name}"

    api.register_tool(RegisteredTool(
        name="{tool_name}",
        description="Test tool from {name}",
        input_schema={{"type": "object", "properties": {{}}}},
        transport=ToolTransport.NATIVE,
        execute=_execute,
        source="extension:{name}",
    ))
"""
    )
    return ext_file


class TestSelfExtension:
    """PRD §4.1 acceptance: write extension → reload → tool available → execute."""

    async def test_write_extension_reload_execute(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Full self-extension lifecycle."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        # Verify no custom tools before extension
        assert "custom_tool" not in agent._tool_registry.tools

        # Write extension to workspace
        ext_dir = workspace / "extensions"
        ext_dir.mkdir()
        _write_extension(ext_dir, "custom_ext", "custom_tool")

        # Reload picks up the new extension
        await agent.reload()

        # Tool is now available
        assert "custom_tool" in agent._tool_registry.tools

        # Tool executes correctly
        tool = agent._tool_registry.tools["custom_tool"]
        result = await tool.execute()
        assert result == "hello from custom_tool"

        await agent.shutdown()

    async def test_extension_tool_survives_in_arcrun_tools(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Extension tools appear in to_arcrun_tools() for LLM."""
        ext_dir = workspace / "extensions"
        ext_dir.mkdir()
        _write_extension(ext_dir, "llm_ext", "llm_tool")

        agent = ArcAgent(config=agent_config)
        await agent.startup()

        arcrun_tools = agent._tool_registry.to_arcrun_tools()
        tool_names = {t.name for t in arcrun_tools}
        assert "llm_tool" in tool_names
        # Built-in tools also present
        assert {"read", "write", "edit", "bash", "grep", "find", "ls"}.issubset(tool_names)

        await agent.shutdown()

    async def test_reload_clears_old_extension_tools(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Reload removes old extension tools before re-discovering."""
        ext_dir = workspace / "extensions"
        ext_dir.mkdir()
        _write_extension(ext_dir, "v1_ext", "v1_tool")

        agent = ArcAgent(config=agent_config)
        await agent.startup()
        assert "v1_tool" in agent._tool_registry.tools

        # Remove old extension, add new
        (ext_dir / "v1_ext.py").unlink()
        _write_extension(ext_dir, "v2_ext", "v2_tool")

        await agent.reload()

        # v1 gone, v2 available
        assert "v1_tool" not in agent._tool_registry.tools
        assert "v2_tool" in agent._tool_registry.tools

        await agent.shutdown()

    async def test_builtin_tools_survive_reload(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Built-in tools (read, write, edit, bash, grep, find, ls) survive reload."""
        agent = ArcAgent(config=agent_config)
        await agent.startup()

        builtin_names = {"read", "write", "edit", "bash", "grep", "find", "ls"}
        for name in builtin_names:
            assert name in agent._tool_registry.tools

        await agent.reload()

        for name in builtin_names:
            assert name in agent._tool_registry.tools, f"{name} missing after reload"

        await agent.shutdown()

    async def test_bad_extension_does_not_crash_agent(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """A broken extension doesn't prevent agent from starting."""
        ext_dir = workspace / "extensions"
        ext_dir.mkdir()

        # Bad extension
        (ext_dir / "bad.py").write_text("def extension(api):\n    raise RuntimeError('boom')\n")

        # Good extension
        _write_extension(ext_dir, "good_ext", "good_tool")

        agent = ArcAgent(config=agent_config)
        await agent.startup()

        # Good tool registered despite bad extension
        assert "good_tool" in agent._tool_registry.tools
        assert agent._started

        await agent.shutdown()

    async def test_extension_audit_events(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Extension loading emits audit events."""
        ext_dir = workspace / "extensions"
        ext_dir.mkdir()
        _write_extension(ext_dir, "audited_ext", "audit_tool")

        with caplog.at_level(logging.INFO, logger="arcagent.audit"):
            agent = ArcAgent(config=agent_config)
            await agent.startup()

        # Audit log should contain extension.loaded event
        audit_records = [r for r in caplog.records if "extension.loaded" in r.getMessage()]
        assert len(audit_records) >= 1
        assert "audited_ext" in audit_records[0].getMessage()

        await agent.shutdown()

    async def test_extension_hooks_work(
        self,
        agent_config: ArcAgentConfig,
        workspace: Path,
    ) -> None:
        """Extensions can subscribe to bus events via api.on()."""
        ext_dir = workspace / "extensions"
        ext_dir.mkdir()

        # Extension that subscribes to an event
        (ext_dir / "hook_ext.py").write_text(
            """
import asyncio

hook_called = asyncio.Event()

def extension(api):
    async def on_event(ctx):
        hook_called.set()

    api.on("agent:post_respond", on_event)
"""
        )

        agent = ArcAgent(config=agent_config)
        await agent.startup()

        # Emit the event
        await agent._bus.emit("agent:post_respond", {"result": "test"})

        # Check that the hook was called
        import sys

        hook_module = sys.modules.get("arcagent_ext_hook_ext")
        assert hook_module is not None
        assert hook_module.hook_called.is_set()

        await agent.shutdown()
