"""Tests for skill_versions and skill_rollback tools."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import ModuleBus, ModuleContext
from arcagent.modules.skill_improver.candidate_store import CandidateStore
from arcagent.modules.skill_improver.models import Candidate
from arcagent.modules.skill_improver.skill_improver_module import SkillImproverModule


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
def tool_registry() -> MagicMock:
    registry = MagicMock()
    registry.register = MagicMock()
    return registry


@pytest.fixture
def module(workspace: Path, telemetry: MagicMock) -> SkillImproverModule:
    return SkillImproverModule(workspace=workspace, telemetry=telemetry)


@pytest.fixture
def bus() -> ModuleBus:
    return ModuleBus()


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


class TestSkillVersionsTool:
    """H1: skill_versions returns version history."""

    @pytest.mark.asyncio
    async def test_no_history(self, module: SkillImproverModule) -> None:
        result = await module._handle_skill_versions("nonexistent")
        assert "No optimization history" in result

    @pytest.mark.asyncio
    async def test_with_candidates(
        self,
        module: SkillImproverModule,
        workspace: Path,
    ) -> None:
        store = CandidateStore(workspace)
        c1 = Candidate(
            id="c1",
            text="# V1\nSteps",
            aggregate_scores={"accuracy": 3.0},
            token_count=5,
            generation=0,
        )
        c2 = Candidate(
            id="c2",
            text="# V2\nBetter steps",
            aggregate_scores={"accuracy": 4.0},
            token_count=6,
            parent_id="c1",
            generation=1,
        )
        store.save("my-skill", c1)
        store.save("my-skill", c2, active=True)

        result = await module._handle_skill_versions("my-skill")
        assert "c1" in result
        assert "c2" in result
        assert "(active)" in result


class TestSkillRollbackTool:
    """H2: skill_rollback reverts to previous version, sets cooloff."""

    @pytest.mark.asyncio
    async def test_rollback_success(
        self,
        module: SkillImproverModule,
        workspace: Path,
    ) -> None:
        store = CandidateStore(workspace)
        c1 = Candidate(id="c1", text="# V1", token_count=3, generation=0)
        c2 = Candidate(id="c2", text="# V2", token_count=3, parent_id="c1", generation=1)
        store.save("my-skill", c1)
        store.save("my-skill", c2, active=True)

        result = await module._handle_skill_rollback("my-skill", "c1")
        assert "Rolled back" in result
        assert "cooloff" in result.lower() or "Cooloff" in result

    @pytest.mark.asyncio
    async def test_rollback_nonexistent(
        self,
        module: SkillImproverModule,
    ) -> None:
        result = await module._handle_skill_rollback("my-skill", "bad-id")
        assert "failed" in result.lower()


class TestToolRegistration:
    """H3: Tools registered with correct schemas."""

    @pytest.mark.asyncio
    async def test_tools_registered(
        self,
        module: SkillImproverModule,
        module_ctx: ModuleContext,
        tool_registry: MagicMock,
    ) -> None:
        await module.startup(module_ctx)
        assert tool_registry.register.call_count == 2

        # Verify tool names
        registered_names = [call.args[0].name for call in tool_registry.register.call_args_list]
        assert "skill_versions" in registered_names
        assert "skill_rollback" in registered_names
