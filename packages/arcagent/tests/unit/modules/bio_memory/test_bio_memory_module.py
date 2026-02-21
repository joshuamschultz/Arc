"""Tests for BioMemoryModule — facade, bus subscriptions, tool registration, mutual exclusivity."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.errors import ConfigError
from arcagent.core.module_bus import EventContext, ModuleBus, ModuleContext
from arcagent.modules.bio_memory.bio_memory_module import BioMemoryModule


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    return tmp_path / "workspace"


@pytest.fixture
def telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


@pytest.fixture
def bus() -> ModuleBus:
    return ModuleBus()


@pytest.fixture
def tool_registry() -> MagicMock:
    return MagicMock()


@pytest.fixture
def module_ctx(
    bus: ModuleBus,
    tool_registry: MagicMock,
    workspace: Path,
    telemetry: MagicMock,
) -> ModuleContext:
    return ModuleContext(
        bus=bus,
        tool_registry=tool_registry,
        config=ArcAgentConfig(
            agent=AgentConfig(name="test-agent"),
            llm=LLMConfig(model="test/model"),
        ),
        telemetry=telemetry,
        workspace=workspace,
        llm_config=LLMConfig(model="test/model"),
    )


@pytest.fixture
def module(workspace: Path, telemetry: MagicMock) -> BioMemoryModule:
    return BioMemoryModule(
        workspace=workspace,
        telemetry=telemetry,
    )


class TestModuleProtocol:
    """BioMemoryModule satisfies the Module protocol."""

    def test_name(self, module: BioMemoryModule) -> None:
        assert module.name == "bio_memory"

    @pytest.mark.asyncio
    async def test_startup_registers_handlers(
        self, module: BioMemoryModule, module_ctx: ModuleContext, bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        # Should have registered handlers for these events
        assert bus.handler_count("agent:assemble_prompt") >= 1
        assert bus.handler_count("agent:post_respond") >= 1
        assert bus.handler_count("agent:pre_tool") >= 1
        assert bus.handler_count("agent:post_tool") >= 1
        assert bus.handler_count("agent:shutdown") >= 1

    @pytest.mark.asyncio
    async def test_startup_registers_tools(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        tool_registry: MagicMock,
    ) -> None:
        await module.startup(module_ctx)
        # Should register 4 tools
        assert tool_registry.register.call_count == 4
        tool_names = [
            call.args[0].name for call in tool_registry.register.call_args_list
        ]
        assert "memory_search" in tool_names
        assert "memory_note" in tool_names
        assert "memory_recall" in tool_names
        assert "memory_reflect" in tool_names

    @pytest.mark.asyncio
    async def test_shutdown_no_error(self, module: BioMemoryModule) -> None:
        await module.shutdown()


class TestMutualExclusivity:
    """Bio-memory and markdown-memory cannot both be active."""

    @pytest.mark.asyncio
    async def test_raises_if_memory_module_present(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        # Register a fake "memory" module
        fake_memory = MagicMock()
        fake_memory.name = "memory"
        bus.register_module(fake_memory)

        with pytest.raises(ConfigError, match="mutually exclusive"):
            await module.startup(module_ctx)


class TestBashVeto:
    """Pre-tool handler vetoes bash commands targeting memory paths."""

    @pytest.mark.asyncio
    async def test_bash_targeting_memory_vetoed(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, workspace: Path,
    ) -> None:
        await module.startup(module_ctx)

        ctx = EventContext(
            event="agent:pre_tool",
            data={
                "tool": "bash",
                "args": {"command": f"rm {workspace}/memory/working.md"},
            },
            agent_did="test",
            trace_id="t1",
        )
        await bus.emit("agent:pre_tool", ctx.data)
        # The emitted context should be vetoed
        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f"cat {workspace}/memory/working.md"},
            },
        )
        assert result.is_vetoed

    @pytest.mark.asyncio
    async def test_bash_not_targeting_memory_allowed(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)

        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": "ls /tmp"},
            },
        )
        assert not result.is_vetoed


class TestBashVetoSecurity:
    """Bash veto uses shlex parsing and handles edge cases."""

    @pytest.mark.asyncio
    async def test_bash_sed_targeting_memory_vetoed(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, workspace: Path,
    ) -> None:
        """sed with memory subpath detected by dangerous command check."""
        await module.startup(module_ctx)
        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f"sed -i 's/old/new/' {workspace}/memory/working.md"},
            },
        )
        assert result.is_vetoed

    @pytest.mark.asyncio
    async def test_bash_malformed_shell_fallback(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        """Malformed shell with memory path still vetoed (fallback to substring)."""
        await module.startup(module_ctx)
        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": "echo 'unclosed memory/ quote"},
            },
        )
        assert result.is_vetoed

    @pytest.mark.asyncio
    async def test_non_bash_tool_not_vetoed(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        result = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "read",
                "args": {"file_path": "memory/working.md"},
            },
        )
        assert not result.is_vetoed


class TestToolHandlers:
    """Tool handlers emit audit events, sanitize input, handle all targets."""

    @pytest.mark.asyncio
    async def test_memory_note_episode_target(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_note with target='episode' creates episode file (T-2 PRD)."""
        # Create the memory/episodes directory
        memory_dir = module._memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)

        result = await module._handle_memory_note(
            content="Important discovery about deployment",
            target="episode",
        )
        assert "Episode created" in result
        episodes_dir = memory_dir / "episodes"
        assert episodes_dir.exists()
        episode_files = list(episodes_dir.glob("*.md"))
        assert len(episode_files) == 1

    @pytest.mark.asyncio
    async def test_memory_note_sanitizes_content(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_note strips zero-width characters (ASI-06)."""
        memory_dir = module._memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)

        # Content with zero-width characters
        poisoned = "Normal text\u200bwith\u200finvisible\ufeffchars"
        result = await module._handle_memory_note(content=poisoned, target="working")
        assert "Note recorded" in result

        text = (memory_dir / "working.md").read_text(encoding="utf-8")
        assert "\u200b" not in text
        assert "\u200f" not in text
        assert "\ufeff" not in text

    @pytest.mark.asyncio
    async def test_memory_search_emits_audit(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_search emits audit event (NIST AU-2)."""
        await module._handle_memory_search(query="test")
        telemetry.audit_event.assert_called()
        call_args = telemetry.audit_event.call_args
        assert "memory.searched" in str(call_args)

    @pytest.mark.asyncio
    async def test_memory_recall_emits_audit(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_recall emits audit event (NIST AU-2)."""
        await module._handle_memory_recall(name="test-entity")
        telemetry.audit_event.assert_called()
        call_args = telemetry.audit_event.call_args
        assert "memory.recalled" in str(call_args)

    @pytest.mark.asyncio
    async def test_memory_note_emits_audit(
        self, module: BioMemoryModule, telemetry: MagicMock,
    ) -> None:
        """memory_note emits audit event for working target (NIST AU-2)."""
        memory_dir = module._memory_dir
        memory_dir.mkdir(parents=True, exist_ok=True)

        await module._handle_memory_note(content="Test note")
        telemetry.audit_event.assert_called()
        call_args = telemetry.audit_event.call_args
        assert "memory.note_written" in str(call_args)


class TestPostToolAudit:
    """_on_post_tool audits write/edit operations targeting memory files."""

    @pytest.mark.asyncio
    async def test_post_tool_audits_write_to_memory(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, workspace: Path, telemetry: MagicMock,
    ) -> None:
        """Write tool targeting memory path emits audit event."""
        await module.startup(module_ctx)
        # Create memory dir so _is_memory_path can resolve
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True, exist_ok=True)

        await bus.emit(
            "agent:post_tool",
            {
                "tool": "write",
                "args": {"file_path": str(memory_dir / "working.md")},
            },
        )
        # Check audit was called with memory.file_modified_by_tool
        calls = [str(c) for c in telemetry.audit_event.call_args_list]
        assert any("memory.file_modified_by_tool" in c for c in calls)

    @pytest.mark.asyncio
    async def test_post_tool_ignores_non_memory_write(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, telemetry: MagicMock,
    ) -> None:
        """Write tool NOT targeting memory does not emit audit."""
        await module.startup(module_ctx)
        telemetry.audit_event.reset_mock()

        await bus.emit(
            "agent:post_tool",
            {
                "tool": "write",
                "args": {"file_path": "/tmp/not-memory.txt"},
            },
        )
        calls = [str(c) for c in telemetry.audit_event.call_args_list]
        assert not any("memory.file_modified_by_tool" in c for c in calls)

    @pytest.mark.asyncio
    async def test_post_tool_ignores_non_write_tools(
        self, module: BioMemoryModule, module_ctx: ModuleContext,
        bus: ModuleBus, telemetry: MagicMock,
    ) -> None:
        """Non-write/edit tools don't trigger audit."""
        await module.startup(module_ctx)
        telemetry.audit_event.reset_mock()

        await bus.emit(
            "agent:post_tool",
            {
                "tool": "read",
                "args": {"file_path": "memory/working.md"},
            },
        )
        calls = [str(c) for c in telemetry.audit_event.call_args_list]
        assert not any("memory.file_modified_by_tool" in c for c in calls)


class TestConstructor:
    """Constructor handles various parameter combinations."""

    def test_default_construction(self, workspace: Path) -> None:
        m = BioMemoryModule(workspace=workspace)
        assert m.name == "bio_memory"

    def test_custom_config(self, workspace: Path) -> None:
        m = BioMemoryModule(
            config={"total_per_turn": 8000, "identity_budget": 1000},
            workspace=workspace,
        )
        assert m.name == "bio_memory"
