"""D2 (SPEC-031) — policy-gated mid-task delivery into a tracked run.

The agent tracks the live ``RunHandle`` for each keyed session and delivers an
incoming teammate message into it: ``follow_up`` by default, ``steer`` only when
the caller is interrupt-eligible AND the arctrust policy pipeline permits it for
that caller. When the agent is idle for that session (no active run), delivery
starts a fresh tracked run instead. REQ-040, REQ-041.
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


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _config(tmp_path: Path, workspace: Path, tier: str = "personal") -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="steer-test-agent",
            org="testorg",
            type="executor",
            workspace=str(workspace),
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
        security=SecurityConfig(tier=tier),  # type: ignore[arg-type]
    )


class _FakeHandle:
    """Records steer/follow_up calls; stands in for a live arcrun RunHandle."""

    def __init__(self) -> None:
        self.steered: list[tuple[str, str]] = []
        self.followed: list[tuple[str, str]] = []

    async def steer(self, caller_did: str, message: str) -> None:
        self.steered.append((caller_did, message))

    async def follow_up(self, caller_did: str, message: str) -> None:
        self.followed.append((caller_did, message))


async def _started_agent(tmp_path: Path, workspace: Path, tier: str = "personal") -> ArcAgent:
    agent = ArcAgent(config=_config(tmp_path, workspace, tier))
    with patch("arcagent.core.model_manager.load_eval_model") as m:
        m.return_value = MagicMock(close=AsyncMock())
        await agent.startup()
    return agent


class TestActiveRunTracking:
    @pytest.mark.asyncio
    async def test_active_run_none_when_idle(self, tmp_path: Path, workspace: Path) -> None:
        agent = await _started_agent(tmp_path, workspace)
        try:
            assert agent.active_run("messaging:inbox") is None
        finally:
            await agent.shutdown()


class TestDeliverToActiveRun:
    @pytest.mark.asyncio
    async def test_default_is_follow_up(self, tmp_path: Path, workspace: Path) -> None:
        """A non-interrupt message is queued via follow_up, never steer."""
        agent = await _started_agent(tmp_path, workspace)
        handle = _FakeHandle()
        agent._active_runs["messaging:inbox"] = handle  # type: ignore[assignment]
        try:
            outcome = await agent.deliver_message(
                caller_did="did:arc:local:peer/aaaa",
                message="fyi",
                session_key="messaging:inbox",
                interrupt=False,
            )
            assert outcome == "followed_up"
            assert handle.followed == [("did:arc:local:peer/aaaa", "fyi")]
            assert handle.steered == []
        finally:
            await agent.shutdown()

    @pytest.mark.asyncio
    async def test_interrupt_steers_when_policy_allows(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        agent = await _started_agent(tmp_path, workspace)
        handle = _FakeHandle()
        agent._active_runs["messaging:inbox"] = handle  # type: ignore[assignment]
        try:
            with patch.object(agent, "_authorize_steer", AsyncMock(return_value=True)):
                outcome = await agent.deliver_message(
                    caller_did="did:arc:local:peer/aaaa",
                    message="URGENT",
                    session_key="messaging:inbox",
                    interrupt=True,
                )
            assert outcome == "steered"
            assert handle.steered == [("did:arc:local:peer/aaaa", "URGENT")]
            assert handle.followed == []
        finally:
            await agent.shutdown()

    @pytest.mark.asyncio
    async def test_interrupt_falls_back_to_follow_up_when_denied(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        """A denied steer must not interrupt — it degrades to follow_up (REQ-041)."""
        agent = await _started_agent(tmp_path, workspace)
        handle = _FakeHandle()
        agent._active_runs["messaging:inbox"] = handle  # type: ignore[assignment]
        try:
            with patch.object(agent, "_authorize_steer", AsyncMock(return_value=False)):
                outcome = await agent.deliver_message(
                    caller_did="did:arc:local:peer/aaaa",
                    message="URGENT",
                    session_key="messaging:inbox",
                    interrupt=True,
                )
            assert outcome == "followed_up"
            assert handle.steered == []
            assert handle.followed == [("did:arc:local:peer/aaaa", "URGENT")]
        finally:
            await agent.shutdown()


class TestDeliverStartsRunWhenIdle:
    @pytest.mark.asyncio
    async def test_idle_starts_tracked_run(self, tmp_path: Path, workspace: Path) -> None:
        agent = await _started_agent(tmp_path, workspace)
        started: dict[str, Any] = {}

        async def _fake_start(inp: str, *, session_key: str) -> _FakeHandle:
            started["input"] = inp
            started["session_key"] = session_key
            h = _FakeHandle()
            agent._active_runs[session_key] = h  # type: ignore[assignment]
            return h

        try:
            with patch.object(agent, "start_tracked_run", side_effect=_fake_start):
                outcome = await agent.deliver_message(
                    caller_did="did:arc:local:peer/aaaa",
                    message="new task",
                    session_key="messaging:inbox",
                    interrupt=False,
                )
            assert outcome == "started"
            assert started == {"input": "new task", "session_key": "messaging:inbox"}
        finally:
            await agent.shutdown()


class TestSteerPolicyGate:
    @pytest.mark.asyncio
    async def test_authorize_steer_denied_is_audited(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        """A pipeline DENY blocks the steer and emits an audit event."""
        agent = await _started_agent(tmp_path, workspace)
        try:
            deny = MagicMock()
            deny.is_deny.return_value = True
            deny.layer = "global"
            deny.rule_id = "global.denylist"
            deny.reason = "steer blocked"
            agent._policy_pipeline = MagicMock()
            agent._policy_pipeline.evaluate = AsyncMock(return_value=deny)
            agent._telemetry = MagicMock()

            allowed = await agent._authorize_steer("did:arc:local:peer/aaaa")
            assert allowed is False
            agent._telemetry.audit_event.assert_called_once()
            assert agent._telemetry.audit_event.call_args.args[0] == "messaging.steer.denied"
        finally:
            await agent.shutdown()

    @pytest.mark.asyncio
    async def test_authorize_steer_allowed(self, tmp_path: Path, workspace: Path) -> None:
        agent = await _started_agent(tmp_path, workspace)
        try:
            allow = MagicMock()
            allow.is_deny.return_value = False
            agent._policy_pipeline = MagicMock()
            agent._policy_pipeline.evaluate = AsyncMock(return_value=allow)
            assert await agent._authorize_steer("did:arc:local:peer/aaaa") is True
        finally:
            await agent.shutdown()
