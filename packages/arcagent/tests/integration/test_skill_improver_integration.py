"""Integration tests for the skill_improver module — end-to-end lifecycle."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from arcagent.core.config import AgentConfig, ArcAgentConfig, LLMConfig
from arcagent.core.module_bus import EventContext, ModuleBus, ModuleContext
from arcagent.core.skill_registry import SkillMeta, SkillRegistry
from arcagent.modules.skill_improver.candidate_store import CandidateStore
from arcagent.modules.skill_improver.config import SkillImproverConfig
from arcagent.modules.skill_improver.guardrails import Guardrails
from arcagent.modules.skill_improver.models import (
    Candidate,
    SkillTrace,
)
from arcagent.modules.skill_improver.skill_improver_module import SkillImproverModule

SKILL_TEXT = """\
## SKILL INTENT [IMMUTABLE]
Plan business travel efficiently.

## Steps
1. Check calendar
2. Book flights
3. Confirm hotel
"""


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


@pytest.fixture
def skill_file(workspace: Path) -> Path:
    skills_dir = workspace / "skills"
    skills_dir.mkdir()
    skill_path = skills_dir / "plan-travel.md"
    skill_path.write_text(SKILL_TEXT, encoding="utf-8")
    return skill_path


@pytest.fixture
def skill_registry(skill_file: Path) -> SkillRegistry:
    registry = SkillRegistry()
    registry._skills["plan-travel"] = SkillMeta(
        name="plan-travel",
        description="Plan business travel",
        file_path=skill_file,
        tags=[],
    )
    return registry


@pytest.fixture
def telemetry() -> MagicMock:
    t = MagicMock()
    t.audit_event = MagicMock()
    return t


def _make_ctx(event: str = "agent:post_tool", **data: object) -> EventContext:
    return EventContext(
        event=event,
        data=dict(data),
        agent_did="did:test:agent",
        trace_id="trace-test",
    )


class TestFullModuleLifecycle:
    """H4: Startup -> collect traces -> trigger optimization -> verify skill updated."""

    @pytest.mark.asyncio
    async def test_trace_collection_via_module(
        self,
        workspace: Path,
        skill_file: Path,
        skill_registry: SkillRegistry,
        telemetry: MagicMock,
    ) -> None:
        """Module collects traces when skills are read."""
        module = SkillImproverModule(
            workspace=workspace,
            telemetry=telemetry,
            config={"optimize_after_uses": 100},
        )

        bus = ModuleBus()
        tool_registry = MagicMock()
        ctx = ModuleContext(
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
        await module.startup(ctx)

        # Simulate ready event with skill registry
        ready_ctx = _make_ctx(event="agent:ready", skill_registry=skill_registry)
        await module._on_ready(ready_ctx)

        # Simulate reading the skill file 5 times
        for _i in range(5):
            read_ctx = _make_ctx(
                tool="read",
                args={"file_path": str(skill_file)},
            )
            await module._on_post_tool(read_ctx)

            # Simulate a tool call within the span
            bash_ctx = _make_ctx(
                tool="bash",
                args={"command": "echo test"},
                duration=0.01,
            )
            await module._on_post_tool(bash_ctx)

            # Close turn
            close_ctx = _make_ctx(event="agent:post_plan")
            await module._on_post_plan(close_ctx)

        # Verify traces were collected
        assert module._collector is not None
        traces = module._collector.load_traces("plan-travel")
        assert len(traces) == 5
        assert module._collector.usage_counts["plan-travel"] == 5


class TestGuardrailEnforcement:
    """H6: Insufficient traces -> no optimization."""

    @pytest.mark.asyncio
    async def test_no_optimization_with_few_traces(
        self,
        workspace: Path,
    ) -> None:
        config = SkillImproverConfig(min_traces=30)
        guardrails = Guardrails(config)
        traces = [
            SkillTrace(
                trace_id=f"t{i}",
                session_id="s1",
                skill_name="test",
                skill_version=0,
                turn_number=i,
                started_at=datetime(2026, 2, 25, 10, 0, 0, tzinfo=UTC),
            )
            for i in range(5)
        ]
        assert guardrails.check_eligible("test", traces) is False


class TestExemptSkill:
    """H7: Tagged security-critical -> never optimized."""

    def test_security_critical_exempt(self) -> None:
        config = SkillImproverConfig(
            min_traces=1,
            exempt_tags=["security-critical", "compliance", "auth"],
        )
        guardrails = Guardrails(config)
        traces = [
            SkillTrace(
                trace_id="t1",
                session_id="s1",
                skill_name="auth-skill",
                skill_version=0,
                turn_number=1,
                started_at=datetime(2026, 2, 25, 10, 0, 0, tzinfo=UTC),
            )
            for _ in range(50)
        ]
        # With security-critical tag, should not be eligible
        assert (
            guardrails.check_eligible(
                "auth-skill",
                traces,
                skill_tags=["security-critical"],
            )
            is False
        )

        # Without exempt tags, should be eligible
        assert (
            guardrails.check_eligible(
                "auth-skill",
                traces,
                skill_tags=["utility"],
            )
            is True
        )


class TestRollbackScenario:
    """H5: Optimize -> rollback -> verify cooloff."""

    @pytest.mark.asyncio
    async def test_rollback_applies_previous_and_cooloff(
        self,
        workspace: Path,
        skill_file: Path,
        telemetry: MagicMock,
    ) -> None:
        store = CandidateStore(workspace)
        module = SkillImproverModule(
            workspace=workspace,
            telemetry=telemetry,
            config={"cooloff_turns": 200},
        )

        # Save two candidates
        c1 = Candidate(id="c1", text="# V1\nOriginal", token_count=3, generation=0)
        c2 = Candidate(
            id="c2",
            text="# V2\nImproved",
            token_count=3,
            parent_id="c1",
            generation=1,
        )
        store.save("plan-travel", c1)
        store.save("plan-travel", c2, active=True)

        # Rollback
        result = await module._handle_skill_rollback("plan-travel", "c1")
        assert "Rolled back" in result

        # Verify active is now c1
        active = store.get_active("plan-travel")
        assert active is not None
        assert active.id == "c1"

        # Verify cooloff is set
        assert module._guardrails.in_cooloff("plan-travel", current_turn=0) is True
