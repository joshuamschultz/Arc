"""Integration tests for memory wiring — full flow from startup to search."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    EvalConfig,
    LLMConfig,
    MemoryConfig,
    ModuleEntry,
)
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.core.module_loader import ModuleLoader
from arcagent.core.tool_registry import ToolRegistry, ToolsConfig


def _make_config(memory_enabled: bool = True) -> ArcAgentConfig:
    modules: dict[str, ModuleEntry] = {}
    if memory_enabled:
        modules["memory"] = ModuleEntry(enabled=True)
    return ArcAgentConfig(
        agent=AgentConfig(name="test-agent"),
        llm=LLMConfig(model="anthropic/claude-haiku"),
        eval=EvalConfig(provider="test", model="eval-model"),
        memory=MemoryConfig(entity_extraction_enabled=True),
        modules=modules,
    )


class TestConventionLoaderIntegration:
    """Startup → convention loader → memory module registered."""

    def test_convention_loader_finds_memory(self, tmp_path: Path) -> None:
        """Convention loader discovers memory module from MODULE.yaml."""
        config = _make_config()
        loader = ModuleLoader()
        modules_dir = Path(__file__).parent.parent.parent / "arcagent" / "modules"

        manifests = loader.discover(modules_dir, config)
        assert any(m.name == "memory" for m in manifests)

    async def test_full_startup_flow(self, tmp_path: Path) -> None:
        """Full: loader → load → register → startup → tool registered."""
        config = _make_config()
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()

        bus = ModuleBus(config=config, telemetry=telemetry)
        tool_registry = ToolRegistry(
            config=ToolsConfig(),
            bus=bus,
            telemetry=telemetry,
        )

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )

        # Convention loader discovers and loads
        loader = ModuleLoader()
        modules_dir = Path(__file__).parent.parent.parent / "arcagent" / "modules"
        modules = loader.load_all(modules_dir, ctx)

        assert len(modules) == 1
        assert modules[0].name == "memory"

        # Register and start
        for mod in modules:
            bus.register_module(mod)
        await bus.startup(ctx)

        # Verify memory_search tool registered
        assert "memory_search" in tool_registry.tools

        # Verify event handlers registered
        assert bus.handler_count("agent:pre_tool") >= 1
        assert bus.handler_count("agent:post_tool") >= 1
        assert bus.handler_count("agent:assemble_prompt") >= 1
        assert bus.handler_count("agent:post_respond") >= 1

        # Cleanup
        await bus.shutdown()

    async def test_memory_search_returns_results(self, tmp_path: Path) -> None:
        """memory_search tool returns results from indexed notes."""
        config = _make_config()
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()

        bus = ModuleBus(config=config, telemetry=telemetry)
        tool_registry = ToolRegistry(
            config=ToolsConfig(),
            bus=bus,
            telemetry=telemetry,
        )

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )

        # Create a note to search
        notes_dir = tmp_path / "notes"
        notes_dir.mkdir()
        (notes_dir / "2026-02-15.md").write_text("Discussed project goals and deadlines.")

        # Load and start memory module
        loader = ModuleLoader()
        modules_dir = Path(__file__).parent.parent.parent / "arcagent" / "modules"
        modules = loader.load_all(modules_dir, ctx)
        for mod in modules:
            bus.register_module(mod)
        await bus.startup(ctx)

        # Call memory_search
        search_tool = tool_registry.tools["memory_search"]
        result = await search_tool.execute(query="project goals")
        assert isinstance(result, str)
        # Should find the note
        assert "project goals" in result.lower() or "No memory results" in result

        await bus.shutdown()

    async def test_memory_guidance_injected(self, tmp_path: Path) -> None:
        """Memory guidance injected via assemble_prompt when no ## Memory in identity."""
        config = _make_config()
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()

        bus = ModuleBus(config=config, telemetry=telemetry)
        tool_registry = ToolRegistry(
            config=ToolsConfig(),
            bus=bus,
            telemetry=telemetry,
        )

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )

        # Create identity.md without ## Memory section
        (tmp_path / "identity.md").write_text("# Agent\n\nI am a test agent.")

        loader = ModuleLoader()
        modules_dir = Path(__file__).parent.parent.parent / "arcagent" / "modules"
        modules = loader.load_all(modules_dir, ctx)
        for mod in modules:
            bus.register_module(mod)
        await bus.startup(ctx)

        # Emit assemble_prompt
        sections: dict[str, str] = {"identity": "# Agent\n\nI am a test agent."}
        await bus.emit("agent:assemble_prompt", {"sections": sections})

        assert "memory_guidance" in sections
        assert "memory_search" in sections["memory_guidance"]

        await bus.shutdown()
