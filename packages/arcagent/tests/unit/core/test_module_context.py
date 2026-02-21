"""Tests for ModuleContext — dependency injection container for module startup."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus, ModuleContext


@pytest.fixture()
def config() -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(name="test"),
        llm=LLMConfig(model="test/model"),
    )


@pytest.fixture()
def mock_telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


@pytest.fixture()
def bus(config: ArcAgentConfig, mock_telemetry: MagicMock) -> ModuleBus:
    return ModuleBus()


class TestModuleContextCreation:
    """T1.1.1: ModuleContext creation with all fields."""

    def test_create_with_all_fields(
        self,
        bus: ModuleBus,
        config: ArcAgentConfig,
        mock_telemetry: MagicMock,
        tmp_path: Path,
    ) -> None:
        tool_registry = MagicMock()
        workspace = tmp_path / "test-workspace"

        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=mock_telemetry,
            workspace=workspace,
            llm_config=config.llm,
        )

        assert ctx.bus is bus
        assert ctx.tool_registry is tool_registry
        assert ctx.config is config
        assert ctx.telemetry is mock_telemetry
        assert ctx.workspace == workspace
        assert ctx.llm_config is config.llm


class TestModuleContextFrozen:
    """T1.1.2: ModuleContext is frozen (cannot mutate attributes)."""

    def test_cannot_set_bus(
        self,
        bus: ModuleBus,
        config: ArcAgentConfig,
        mock_telemetry: MagicMock,
        tmp_path: Path,
    ) -> None:
        ctx = ModuleContext(
            bus=bus,
            tool_registry=MagicMock(),
            config=config,
            telemetry=mock_telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )
        with pytest.raises(AttributeError):
            ctx.bus = MagicMock()  # type: ignore[misc]

    def test_cannot_set_config(
        self,
        bus: ModuleBus,
        config: ArcAgentConfig,
        mock_telemetry: MagicMock,
        tmp_path: Path,
    ) -> None:
        ctx = ModuleContext(
            bus=bus,
            tool_registry=MagicMock(),
            config=config,
            telemetry=mock_telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )
        with pytest.raises(AttributeError):
            ctx.config = MagicMock()  # type: ignore[misc]


class TestModuleContextFieldAccess:
    """T1.1.3: ModuleContext fields are accessible."""

    def test_access_llm_config_model(
        self,
        bus: ModuleBus,
        config: ArcAgentConfig,
        mock_telemetry: MagicMock,
        tmp_path: Path,
    ) -> None:
        ctx = ModuleContext(
            bus=bus,
            tool_registry=MagicMock(),
            config=config,
            telemetry=mock_telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )
        assert ctx.llm_config.model == "test/model"

    def test_tool_registry_is_callable(
        self,
        bus: ModuleBus,
        config: ArcAgentConfig,
        mock_telemetry: MagicMock,
        tmp_path: Path,
    ) -> None:
        """Modules can call tool_registry.register() on the shared reference."""
        tool_registry = MagicMock()
        ctx = ModuleContext(
            bus=bus,
            tool_registry=tool_registry,
            config=config,
            telemetry=mock_telemetry,
            workspace=tmp_path,
            llm_config=config.llm,
        )
        # Should be able to call methods on the reference
        ctx.tool_registry.register("test_tool")
        tool_registry.register.assert_called_once_with("test_tool")
