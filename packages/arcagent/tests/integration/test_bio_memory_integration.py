"""Integration test: bio-memory module lifecycle through the module bus.

Tests the full wiring: BioMemoryModule startup/shutdown, bus event dispatch,
working memory lifecycle, identity injection, and mutual exclusivity.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.errors import ConfigError
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.modules.bio_memory.bio_memory_module import BioMemoryModule


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


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


class TestModuleLifecycle:
    """Module starts up, registers handlers, and shuts down cleanly."""

    @pytest.mark.asyncio
    async def test_startup_registers_all_events(
        self, workspace: Path, telemetry: MagicMock,
        bus: ModuleBus, module_ctx: ModuleContext,
    ) -> None:
        module = BioMemoryModule(workspace=workspace, telemetry=telemetry)
        await module.startup(module_ctx)

        assert bus.handler_count("agent:assemble_prompt") >= 1
        assert bus.handler_count("agent:post_respond") >= 1
        assert bus.handler_count("agent:pre_tool") >= 1
        assert bus.handler_count("agent:post_tool") >= 1
        assert bus.handler_count("agent:shutdown") >= 1

    @pytest.mark.asyncio
    async def test_startup_shutdown_cycle(
        self, workspace: Path, telemetry: MagicMock,
        bus: ModuleBus, module_ctx: ModuleContext,
    ) -> None:
        module = BioMemoryModule(workspace=workspace, telemetry=telemetry)
        await module.startup(module_ctx)
        await module.shutdown()


class TestWorkingMemoryLifecycle:
    """Working memory is written during session and cleared on shutdown."""

    @pytest.mark.asyncio
    async def test_assemble_prompt_with_no_files(
        self, workspace: Path, telemetry: MagicMock,
        bus: ModuleBus, module_ctx: ModuleContext,
    ) -> None:
        module = BioMemoryModule(workspace=workspace, telemetry=telemetry)
        await module.startup(module_ctx)

        # Emit assemble_prompt — should not crash even with no files
        ctx = await bus.emit("agent:assemble_prompt", {})
        assert not ctx.is_vetoed

    @pytest.mark.asyncio
    async def test_assemble_prompt_with_identity(
        self, workspace: Path, telemetry: MagicMock,
        bus: ModuleBus, module_ctx: ModuleContext,
    ) -> None:
        # Create identity file
        memory_dir = workspace / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "how-i-work.md").write_text(
            "I prefer structured responses.", encoding="utf-8",
        )

        module = BioMemoryModule(workspace=workspace, telemetry=telemetry)
        await module.startup(module_ctx)

        ctx = await bus.emit("agent:assemble_prompt", {})
        # Should inject memory_context
        assert "memory_context" in ctx.data
        assert "structured responses" in ctx.data["memory_context"]


class TestMutualExclusivity:
    """Bio-memory and markdown-memory cannot coexist."""

    @pytest.mark.asyncio
    async def test_rejects_when_memory_module_registered(
        self, workspace: Path, telemetry: MagicMock,
        bus: ModuleBus, module_ctx: ModuleContext,
    ) -> None:
        # Register the markdown-memory module first
        fake_memory = MagicMock()
        fake_memory.name = "memory"
        bus.register_module(fake_memory)

        module = BioMemoryModule(workspace=workspace, telemetry=telemetry)
        with pytest.raises(ConfigError, match="mutually exclusive"):
            await module.startup(module_ctx)


class TestBashVetoIntegration:
    """Bash commands targeting memory/ are vetoed through the bus."""

    @pytest.mark.asyncio
    async def test_bash_memory_path_vetoed(
        self, workspace: Path, telemetry: MagicMock,
        bus: ModuleBus, module_ctx: ModuleContext,
    ) -> None:
        module = BioMemoryModule(workspace=workspace, telemetry=telemetry)
        await module.startup(module_ctx)

        ctx = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": f"rm -rf {workspace}/memory/episodes/"},
            },
        )
        assert ctx.is_vetoed

    @pytest.mark.asyncio
    async def test_non_memory_bash_allowed(
        self, workspace: Path, telemetry: MagicMock,
        bus: ModuleBus, module_ctx: ModuleContext,
    ) -> None:
        module = BioMemoryModule(workspace=workspace, telemetry=telemetry)
        await module.startup(module_ctx)

        ctx = await bus.emit(
            "agent:pre_tool",
            {
                "tool": "bash",
                "args": {"command": "echo hello"},
            },
        )
        assert not ctx.is_vetoed
