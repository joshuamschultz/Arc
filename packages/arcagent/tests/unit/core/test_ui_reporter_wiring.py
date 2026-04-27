"""Tests for UIReporter wiring into arcagent's tool, module, skill, and memory layers.

Verifies that emit_agent_event fires for:
  - tool_call  (ToolRegistry._create_wrapped_execute)
  - module_lifecycle  (ModuleBus.startup / shutdown)
  - skill_load  (SkillRegistry.discover)
  - extension_load  (ExtensionLoader._run_factory)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import ToolConfig, ToolsConfig
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.core.skill_registry import SkillRegistry
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport

# ---------------------------------------------------------------------------
# Fake reporter — records all emit_agent_event calls
# ---------------------------------------------------------------------------


class FakeReporter:
    """Duck-typed UIEventReporter stub that records emit_agent_event calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def emit_agent_event(self, *, event_type: str, data: dict[str, Any]) -> None:
        self.calls.append((event_type, dict(data)))

    def call_event_types(self) -> list[str]:
        return [et for et, _ in self.calls]

    def first_of(self, event_type: str) -> dict[str, Any] | None:
        for et, data in self.calls:
            if et == event_type:
                return data
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_tool(name: str = "echo") -> RegisteredTool:
    async def _exec(**kwargs: Any) -> str:
        return "ok"

    return RegisteredTool(
        name=name,
        description="test tool",
        input_schema={"type": "object", "properties": {}},
        transport=ToolTransport.NATIVE,
        execute=_exec,
        timeout_seconds=5,
    )


def _make_registry(reporter: Any | None = None) -> ToolRegistry:
    config = ToolsConfig(policy=ToolConfig(allow=[], deny=[]))
    bus = ModuleBus()
    telemetry = MagicMock()
    telemetry.tool_span = MagicMock(return_value=_async_nullcontext())
    telemetry.audit_event = MagicMock()
    return ToolRegistry(
        config=config,
        bus=bus,
        telemetry=telemetry,
        agent_did="did:arc:test:executor/agent1",
        tier="personal",
        ui_reporter=reporter,
    )


class _AsyncNullContextManager:
    async def __aenter__(self) -> None:
        return None

    async def __aexit__(self, *args: Any) -> None:
        pass


def _async_nullcontext() -> _AsyncNullContextManager:
    return _AsyncNullContextManager()


# ---------------------------------------------------------------------------
# Task A-1: ToolRegistry emits tool_call
# ---------------------------------------------------------------------------


class TestToolRegistryUIReporter:
    """ToolRegistry._create_wrapped_execute calls ui_reporter.emit_agent_event."""

    @pytest.mark.asyncio
    async def test_tool_dispatch_emits_tool_call_event(self) -> None:
        reporter = FakeReporter()
        registry = _make_registry(reporter=reporter)
        tool = _make_tool("echo")
        registry.register(tool)

        # Execute the wrapped tool
        wrapped = registry._create_wrapped_execute(tool)
        await wrapped({})

        assert "tool_call" in reporter.call_event_types(), (
            f"Expected tool_call event but got: {reporter.call_event_types()}"
        )

    @pytest.mark.asyncio
    async def test_tool_call_event_data_has_required_fields(self) -> None:
        reporter = FakeReporter()
        registry = _make_registry(reporter=reporter)
        tool = _make_tool("my_tool")
        registry.register(tool)

        wrapped = registry._create_wrapped_execute(tool)
        await wrapped({})

        data = reporter.first_of("tool_call")
        assert data is not None
        assert data["tool_name"] == "my_tool"
        assert "actor_did" in data
        assert "outcome" in data

    @pytest.mark.asyncio
    async def test_no_reporter_does_not_crash(self) -> None:
        """Registry with ui_reporter=None must still execute tools normally."""
        registry = _make_registry(reporter=None)
        tool = _make_tool("no_reporter_tool")
        registry.register(tool)

        wrapped = registry._create_wrapped_execute(tool)
        result = await wrapped({})
        assert result == "ok"

    @pytest.mark.asyncio
    async def test_reporter_receives_actor_did(self) -> None:
        reporter = FakeReporter()
        registry = _make_registry(reporter=reporter)
        tool = _make_tool("actor_tool")
        registry.register(tool)

        wrapped = registry._create_wrapped_execute(tool)
        await wrapped({})

        data = reporter.first_of("tool_call")
        assert data is not None
        assert data["actor_did"] == "did:arc:test:executor/agent1"


# ---------------------------------------------------------------------------
# Task A-2: ModuleBus emits module_lifecycle
# ---------------------------------------------------------------------------


class _StartupModule:
    """Minimal module that records its lifecycle calls."""

    def __init__(self, name: str = "test_module") -> None:
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def startup(self, ctx: ModuleContext) -> None:
        pass

    async def shutdown(self) -> None:
        pass


class TestModuleBusUIReporter:
    """ModuleBus.startup/shutdown emit module_lifecycle events to ui_reporter."""

    @pytest.mark.asyncio
    async def test_module_startup_emits_lifecycle_event(self, tmp_path: Path) -> None:
        reporter = FakeReporter()
        bus = ModuleBus(ui_reporter=reporter)
        module = _StartupModule("alpha")
        bus.register_module(module)

        ctx = _make_module_context(bus, tmp_path)
        await bus.startup(ctx)

        assert "module_lifecycle" in reporter.call_event_types(), (
            f"Expected module_lifecycle event but got: {reporter.call_event_types()}"
        )

    @pytest.mark.asyncio
    async def test_module_startup_event_data_has_name_and_phase(
        self, tmp_path: Path
    ) -> None:
        reporter = FakeReporter()
        bus = ModuleBus(ui_reporter=reporter)
        bus.register_module(_StartupModule("beta"))

        ctx = _make_module_context(bus, tmp_path)
        await bus.startup(ctx)

        data = reporter.first_of("module_lifecycle")
        assert data is not None
        assert data["module_name"] == "beta"
        assert data["phase"] == "start"

    @pytest.mark.asyncio
    async def test_module_shutdown_emits_lifecycle_event(self, tmp_path: Path) -> None:
        reporter = FakeReporter()
        bus = ModuleBus(ui_reporter=reporter)
        bus.register_module(_StartupModule("gamma"))

        ctx = _make_module_context(bus, tmp_path)
        await bus.startup(ctx)
        reporter.calls.clear()
        await bus.shutdown()

        assert "module_lifecycle" in reporter.call_event_types()
        data = reporter.first_of("module_lifecycle")
        assert data is not None
        assert data["phase"] == "stop"

    @pytest.mark.asyncio
    async def test_no_reporter_does_not_crash_on_startup(self, tmp_path: Path) -> None:
        bus = ModuleBus(ui_reporter=None)
        bus.register_module(_StartupModule("no_reporter"))
        ctx = _make_module_context(bus, tmp_path)
        await bus.startup(ctx)  # Must not raise


def _make_module_context(bus: ModuleBus, workspace: Path) -> ModuleContext:
    tool_registry = MagicMock()
    config = MagicMock()
    config.agent.name = "test"
    config.modules = {}
    telemetry = MagicMock()
    llm_config = MagicMock()
    return ModuleContext(
        bus=bus,
        tool_registry=tool_registry,
        config=config,
        telemetry=telemetry,
        workspace=workspace,
        llm_config=llm_config,
    )


# ---------------------------------------------------------------------------
# Task A-3: SkillRegistry emits skill_load
# ---------------------------------------------------------------------------


class TestSkillRegistryUIReporter:
    """SkillRegistry.discover emits skill_load for each skill found."""

    def _make_skill_file(self, directory: Path, name: str) -> Path:
        skill_path = directory / f"{name}.md"
        skill_path.write_text(
            "---\n"
            f"name: {name}\n"
            "description: Test skill\n"
            "version: 1.0.0\n"
            "---\n\nSkill body.\n",
            encoding="utf-8",
        )
        return skill_path

    def test_discover_emits_skill_load_per_skill(self, tmp_path: Path) -> None:
        reporter = FakeReporter()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        self._make_skill_file(skills_dir, "code_review")
        self._make_skill_file(skills_dir, "testing")

        registry = SkillRegistry(ui_reporter=reporter)
        registry.discover(workspace=tmp_path, global_dir=Path("/nonexistent"))

        event_types = reporter.call_event_types()
        skill_loads = [et for et in event_types if et == "skill_load"]
        assert len(skill_loads) == 2, (
            f"Expected 2 skill_load events, got {len(skill_loads)}: {event_types}"
        )

    def test_skill_load_event_has_skill_name(self, tmp_path: Path) -> None:
        reporter = FakeReporter()
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        self._make_skill_file(skills_dir, "planning")

        registry = SkillRegistry(ui_reporter=reporter)
        registry.discover(workspace=tmp_path, global_dir=Path("/nonexistent"))

        data = reporter.first_of("skill_load")
        assert data is not None
        assert data["skill_name"] == "planning"

    def test_no_reporter_does_not_crash(self, tmp_path: Path) -> None:
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir()
        self._make_skill_file(skills_dir, "summary")

        registry = SkillRegistry(ui_reporter=None)
        skills = registry.discover(workspace=tmp_path, global_dir=Path("/nonexistent"))
        assert len(skills) == 1


# ---------------------------------------------------------------------------
# Task A-4: ExtensionLoader emits extension_load
# ---------------------------------------------------------------------------


class TestExtensionLoaderUIReporter:
    """ExtensionLoader._run_factory emits extension_load on success."""

    def _make_extension_file(self, directory: Path, name: str) -> Path:
        ext_path = directory / f"{name}.py"
        ext_path.write_text(
            "def extension(api):\n    pass\n",
            encoding="utf-8",
        )
        return ext_path

    @pytest.mark.asyncio
    async def test_extension_load_emits_event(self, tmp_path: Path) -> None:
        from arcagent.core.config import ExtensionConfig
        from arcagent.core.extensions import ExtensionLoader

        reporter = FakeReporter()
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        self._make_extension_file(ext_dir, "my_ext")

        tool_registry = _make_registry()
        bus = ModuleBus()
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()

        loader = ExtensionLoader(
            tool_registry=tool_registry,
            bus=bus,
            telemetry=telemetry,
            config=ExtensionConfig(),
            ui_reporter=reporter,
        )
        await loader.discover_and_load(workspace=tmp_path, global_dir=Path("/nonexistent"))

        assert "extension_load" in reporter.call_event_types(), (
            f"Expected extension_load event, got: {reporter.call_event_types()}"
        )

    @pytest.mark.asyncio
    async def test_extension_load_event_has_name(self, tmp_path: Path) -> None:
        from arcagent.core.config import ExtensionConfig
        from arcagent.core.extensions import ExtensionLoader

        reporter = FakeReporter()
        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        self._make_extension_file(ext_dir, "vault_helper")

        tool_registry = _make_registry()
        bus = ModuleBus()
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()

        loader = ExtensionLoader(
            tool_registry=tool_registry,
            bus=bus,
            telemetry=telemetry,
            config=ExtensionConfig(),
            ui_reporter=reporter,
        )
        await loader.discover_and_load(workspace=tmp_path, global_dir=Path("/nonexistent"))

        data = reporter.first_of("extension_load")
        assert data is not None
        assert data["extension_name"] == "vault_helper"

    @pytest.mark.asyncio
    async def test_no_reporter_does_not_crash(self, tmp_path: Path) -> None:
        from arcagent.core.config import ExtensionConfig
        from arcagent.core.extensions import ExtensionLoader

        ext_dir = tmp_path / "extensions"
        ext_dir.mkdir()
        self._make_extension_file(ext_dir, "quiet_ext")

        tool_registry = _make_registry()
        bus = ModuleBus()
        telemetry = MagicMock()
        telemetry.audit_event = MagicMock()

        loader = ExtensionLoader(
            tool_registry=tool_registry,
            bus=bus,
            telemetry=telemetry,
            config=ExtensionConfig(),
            ui_reporter=None,
        )
        # Must not raise
        await loader.discover_and_load(workspace=tmp_path, global_dir=Path("/nonexistent"))
