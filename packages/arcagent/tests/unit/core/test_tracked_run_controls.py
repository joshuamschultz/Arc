"""SPEC-043 F1 — the tracked/steerable run path applies the SAME loop controls.

``start_tracked_run`` drives ``arcrun.run_async``; before the fix it omitted
``build_loop_controls``, so the proactive-HITL tier ladder and the
runaway/cascade breakers were silently OFF on the tracked path (reachable via
``agent.start_tracked_run`` and ``deliver_message``). These tests pin that the
tracked path forwards the resolved approval set + breaker floors + checkpoint
hook identically to ``dispatch_stream``, and that the bound approval provider
fails closed with no grant.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    SecurityConfig,
    TelemetryConfig,
)
from arcagent.tools._transport import RegisteredTool, ToolTransport


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="tracked-controls-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
        # enterprise → every plain tool requires approval; breakers set explicitly.
        security=SecurityConfig(
            tier="enterprise",
            custody="in_process",
            runaway_max_repeat=8,
            error_cascade_max=5,
        ),
    )


class _FakeResult:
    content = ""


class _FakeHandle:
    """Stands in for an arcrun RunHandle so the finalizer can await result()."""

    async def result(self) -> _FakeResult:
        return _FakeResult()


def _flagged_tool() -> RegisteredTool:
    return RegisteredTool(
        name="send_email",
        description="send an email",
        input_schema={"type": "object", "properties": {}},
        transport=ToolTransport.NATIVE,
        execute=lambda **_k: "ok",
        skill_backed=False,
    )


async def _started_agent(tmp_path: Path, workspace: Path) -> ArcAgent:
    agent = ArcAgent(config=_config(tmp_path, workspace))
    with patch("arcagent.core.model_manager.load_eval_model") as m:
        m.return_value = MagicMock(close=AsyncMock())
        await agent.startup()
    # A flagged plain tool so the enterprise approval set is non-empty.
    agent._tool_registry.register(_flagged_tool())  # type: ignore[union-attr]
    # Cache a stub run model so _ensure_model short-circuits (no real provider).
    agent._model = MagicMock(close=AsyncMock())
    return agent


async def _capture_run_async_kwargs(agent: ArcAgent) -> dict[str, Any]:
    """Drive start_tracked_run, capturing what arcrun.run_async received."""
    captured: dict[str, Any] = {}

    async def _fake_run_async(*args: Any, **kwargs: Any) -> _FakeHandle:
        captured.update(kwargs)
        return _FakeHandle()

    with patch("arcagent.core.agent_dispatch.arcrun_run_async", side_effect=_fake_run_async):
        await agent.start_tracked_run("do it", session_key="tracked:1")
    return captured


class TestTrackedPathAppliesLoopControls:
    @pytest.mark.asyncio
    async def test_forwards_approval_set_breakers_and_checkpoint(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        agent = await _started_agent(tmp_path, workspace)
        try:
            captured = await _capture_run_async_kwargs(agent)
        finally:
            await agent.shutdown()

        # The enterprise approval set (all plain tools) reaches the tracked loop.
        assert "send_email" in captured["approval_required_tools"]
        # A provider is bound (proactive HITL is ON, not None).
        assert captured["approval_provider"] is not None
        # The runaway + cascade breakers are ACTIVE on this path.
        assert captured["max_repeat"] == 8
        assert captured["max_consecutive_errors"] == 5
        # Checkpoint hook is wired (crash recovery available on this path too).
        assert captured["on_checkpoint"] is not None

    @pytest.mark.asyncio
    async def test_matches_build_loop_controls_uniformity(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        """The tracked path forwards EXACTLY the helper's controls (uniformity)."""
        from arcagent.tools.approval_policy import build_loop_controls

        agent = await _started_agent(tmp_path, workspace)
        try:
            captured = await _capture_run_async_kwargs(agent)
            session = await agent.session("tracked:1")
            expected = build_loop_controls(agent, session)
        finally:
            await agent.shutdown()

        for key in (
            "approval_required_tools",
            "max_repeat",
            "max_consecutive_errors",
            "max_parallel",
        ):
            assert captured[key] == expected[key]
        # Provider + checkpoint hook are both bound callables on the tracked path.
        assert callable(captured["approval_provider"])
        assert callable(captured["on_checkpoint"])

    @pytest.mark.asyncio
    async def test_bound_provider_fails_closed_without_grant(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        """The provider bound on the tracked path denies (None) with no grant."""
        agent = await _started_agent(tmp_path, workspace)
        try:
            captured = await _capture_run_async_kwargs(agent)
            provider = captured["approval_provider"]
            call = MagicMock(name="send_email", arguments={})
            call.name = "send_email"
            grant = await provider(call)
        finally:
            await agent.shutdown()
        # No channel, no auto-approve → fail closed.
        assert grant is None
