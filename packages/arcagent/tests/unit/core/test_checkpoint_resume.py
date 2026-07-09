"""SPEC-043 F5+F3 — checkpoint resume is wired end-to-end AND tamper-evident.

F5: the WRITE path (``persist_checkpoint`` every turn) had no consumer — nothing
read ``latest_checkpoint`` back into a run. ``resume_stream`` closes that: it
reloads the latest persisted checkpoint and re-enters the REAL ``arcrun.run_stream``
loop with ``resume_from``, restoring ``turn_count`` / ``tokens_used`` so an
interrupted run continues to completion without redoing work.

F3: the checkpoint restores the budget counters, so it is signed by the operator
key and verified on resume — a tampered or unsigned checkpoint fails closed, an
agent cannot zero ``tokens_used`` to reset the LLM10 breaker on resume.

These tests drive the production path (a real MockModel through the real loop),
crash after a persisted checkpoint, then resume via a FRESH agent that reads the
checkpoint back from disk.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from arcrun import TurnEndEvent

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
from arcagent.tools.checkpoint_resume import resume_stream


@dataclass
class Usage:
    input_tokens: int = 10
    output_tokens: int = 5
    total_tokens: int = 15


@dataclass
class LLMResponse:
    content: str | None = None
    tool_calls: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"
    usage: Usage = field(default_factory=Usage)
    cost_usd: float = 0.0


class MockModel:
    """Scripted arcllm model — drives the REAL react loop to a known outcome."""

    def __init__(self, responses: list[LLMResponse]) -> None:
        self._responses = list(responses)
        self._i = 0

    async def invoke(self, messages: list, tools: list | None = None) -> LLMResponse:
        if self._i >= len(self._responses):
            raise RuntimeError("MockModel exhausted responses")
        resp = self._responses[self._i]
        self._i += 1
        return resp

    async def close(self) -> None:
        return None


_SESSION_KEY = "resume:job"


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ws


def _config(tmp_path: Path, workspace: Path) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="resume-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
        # in_process operator key in a tmp dir so both the crashed + restarted
        # agent share the same signer (the checkpoint verifies across restart).
        security=SecurityConfig(tier="personal", operator_key_dir=str(tmp_path / "operator")),
    )


async def _started_agent(tmp_path: Path, workspace: Path, model: MockModel) -> ArcAgent:
    agent = ArcAgent(config=_config(tmp_path, workspace))
    with patch("arcagent.core.model_manager.load_eval_model") as m:
        m.return_value = MagicMock(close=AsyncMock())
        await agent.startup()
    agent._model = model  # drive the REAL loop with a scripted model
    return agent


def _resp(text: str) -> LLMResponse:
    return LLMResponse(
        content=text,
        stop_reason="end_turn",
        usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15),
    )


async def _run_first_turn_and_crash(agent: ArcAgent) -> None:
    """Drive one real turn (persists a signed checkpoint), then let it settle."""
    session = await agent.session(_SESSION_KEY)
    async for _ in agent.run("start the job", session=session):
        pass
    # persist_checkpoint is scheduled off the hot path — let it flush to disk.
    for _ in range(10):
        await asyncio.sleep(0.02)
        if session.latest_checkpoint() is not None:
            break


def _session_path(workspace: Path) -> Path:
    return workspace / "sessions" / f"{_SESSION_KEY}.jsonl"


def _read_checkpoint_line(workspace: Path) -> dict:
    for line in _session_path(workspace).read_text().strip().split("\n"):
        rec = json.loads(line)
        if rec.get("type") == "checkpoint":
            return rec
    raise AssertionError("no checkpoint persisted")


class TestResumeToCompletion:
    @pytest.mark.asyncio
    async def test_crash_then_resume_completes_via_production_path(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        # Crash: first agent runs turn 1 (persists a signed checkpoint), then dies.
        agent1 = await _started_agent(tmp_path, workspace, MockModel([_resp("progress")]))
        try:
            await _run_first_turn_and_crash(agent1)
        finally:
            await agent1.shutdown()

        checkpoint = _read_checkpoint_line(workspace)
        assert checkpoint["turn_count"] == 1
        assert checkpoint["tokens_used"]["total"] == 15
        assert checkpoint["signature"]  # signed at persist time

        # Restart: a fresh agent reads the checkpoint from disk and resumes.
        agent2 = await _started_agent(tmp_path, workspace, MockModel([_resp("all done")]))
        try:
            events = [ev async for ev in resume_stream(agent2, session_key=_SESSION_KEY)]
        finally:
            await agent2.shutdown()

        turn_end = next(e for e in events if isinstance(e, TurnEndEvent))
        assert turn_end.final_text == "all done"  # the run continued to completion
        # State was RESTORED, not reset: turn 1 already ran, so this is turn 2 and
        # the restored 15 tokens accumulate with the resumed turn's 15 → 30.
        assert turn_end.turns == 2
        assert turn_end.tokens_used["total"] == 30

    @pytest.mark.asyncio
    async def test_no_checkpoint_is_noop(self, tmp_path: Path, workspace: Path) -> None:
        agent = await _started_agent(tmp_path, workspace, MockModel([]))
        try:
            events = [ev async for ev in resume_stream(agent, session_key="never:ran")]
        finally:
            await agent.shutdown()
        assert events == []  # nothing persisted → empty stream, no loop entered


class TestTamperFailsClosed:
    @pytest.mark.asyncio
    async def test_tampered_tokens_refuse_resume(self, tmp_path: Path, workspace: Path) -> None:
        """Zeroing tokens_used to reset the budget breaker fails closed (LLM10)."""
        agent1 = await _started_agent(tmp_path, workspace, MockModel([_resp("progress")]))
        try:
            await _run_first_turn_and_crash(agent1)
        finally:
            await agent1.shutdown()

        # Tamper: rewrite the checkpoint with tokens zeroed, signature untouched.
        path = _session_path(workspace)
        lines = path.read_text().strip().split("\n")
        out = []
        for line in lines:
            rec = json.loads(line)
            if rec.get("type") == "checkpoint":
                rec["tokens_used"] = {"input": 0, "output": 0, "total": 0}
            out.append(json.dumps(rec))
        path.write_text("\n".join(out) + "\n")

        agent2 = await _started_agent(tmp_path, workspace, MockModel([_resp("all done")]))
        try:
            with pytest.raises(ValueError, match="tampered|verification failed"):
                async for _ in resume_stream(agent2, session_key=_SESSION_KEY):
                    pass
        finally:
            await agent2.shutdown()

    @pytest.mark.asyncio
    async def test_unsigned_checkpoint_refuses_resume(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        """A checkpoint with no signature is refused — no unsigned resume."""
        agent1 = await _started_agent(tmp_path, workspace, MockModel([_resp("progress")]))
        try:
            await _run_first_turn_and_crash(agent1)
        finally:
            await agent1.shutdown()

        path = _session_path(workspace)
        lines = path.read_text().strip().split("\n")
        out = []
        for line in lines:
            rec = json.loads(line)
            if rec.get("type") == "checkpoint":
                rec["signature"] = None
            out.append(json.dumps(rec))
        path.write_text("\n".join(out) + "\n")

        agent2 = await _started_agent(tmp_path, workspace, MockModel([_resp("all done")]))
        try:
            with pytest.raises(ValueError, match="unsigned"):
                async for _ in resume_stream(agent2, session_key=_SESSION_KEY):
                    pass
        finally:
            await agent2.shutdown()
