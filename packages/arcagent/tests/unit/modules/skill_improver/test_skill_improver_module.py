"""Tests for SkillImproverModule — facade, bus subscriptions, protocol compliance."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.modules.skill_improver.skill_improver_module import SkillImproverModule


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
def module(workspace: Path, telemetry: MagicMock) -> SkillImproverModule:
    return SkillImproverModule(
        workspace=workspace,
        telemetry=telemetry,
    )


class TestModuleProtocol:
    """SkillImproverModule satisfies the Module protocol."""

    def test_name(self, module: SkillImproverModule) -> None:
        assert module.name == "skill_improver"

    @pytest.mark.asyncio
    async def test_startup_registers_handlers(
        self,
        module: SkillImproverModule,
        module_ctx: ModuleContext,
        bus: ModuleBus,
    ) -> None:
        await module.startup(module_ctx)
        assert bus.handler_count("agent:post_tool") >= 1
        assert bus.handler_count("agent:post_plan") >= 1
        assert bus.handler_count("agent:post_respond") >= 1
        assert bus.handler_count("agent:ready") >= 1

    @pytest.mark.asyncio
    async def test_shutdown_completes_without_error(
        self,
        module: SkillImproverModule,
    ) -> None:
        await module.shutdown()

    def test_config_defaults(self, module: SkillImproverModule) -> None:
        assert module._config.min_traces == 30
        assert module._config.optimize_after_uses == 50

    def test_config_custom(self, workspace: Path, telemetry: MagicMock) -> None:
        m = SkillImproverModule(
            config={"min_traces": 50, "max_iterations": 20},
            workspace=workspace,
            telemetry=telemetry,
        )
        assert m._config.min_traces == 50
        assert m._config.max_iterations == 20


class TestModuleConstruction:
    """Constructor injection patterns."""

    def test_default_workspace(self) -> None:
        m = SkillImproverModule()
        assert m.name == "skill_improver"

    def test_workspace_resolved(self, tmp_path: Path) -> None:
        ws = tmp_path / "relative" / "path"
        m = SkillImproverModule(workspace=ws)
        assert m._workspace.is_absolute()
