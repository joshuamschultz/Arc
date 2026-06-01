"""Phase D (SPEC-027 AC-5.2) — SPEC-026 recording survives the new run path.

A turn driven through the single streaming ``agent.run`` still records run
lifecycle events to the arcstore operational spool, carrying the agent's DID as
the actor. Routing every surface through one entry strengthens this — no surface
can bypass recording.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from arcagent.core.agent import ArcAgent
from arcagent.core.config import (
    AgentConfig,
    ArcAgentConfig,
    ContextConfig,
    IdentityConfig,
    LLMConfig,
    TelemetryConfig,
)

from .orchestration._mock_llm import LLMResponse, MockModel


@pytest.fixture()
def agent_config(tmp_path: Path) -> ArcAgentConfig:
    ws = tmp_path / "workspace"
    ws.mkdir()
    return ArcAgentConfig(
        agent=AgentConfig(name="record-agent", org="testorg", type="executor", workspace=str(ws)),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=False),
        context=ContextConfig(max_tokens=10000),
    )


@pytest.mark.asyncio
@patch("arcagent.core.model_manager.load_eval_model")
async def test_run_records_run_events_to_spool(
    mock_load_model: MagicMock,
    agent_config: ArcAgentConfig,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A turn through agent.run spools run_event records tagged with the agent DID."""
    store_dir = tmp_path / "store"
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(store_dir))

    model = MockModel([LLMResponse(content="done", stop_reason="end_turn") for _ in range(5)])
    model.close = AsyncMock()  # type: ignore[attr-defined]  # shutdown closes the model
    mock_load_model.return_value = model

    agent = ArcAgent(config=agent_config)
    await agent.startup()
    agent_did = agent._identity.did if agent._identity else ""
    try:
        session = await agent.session("e2e")
        async for _ in agent.run("record this turn", session=session):
            pass
    finally:
        await agent.shutdown()

    from arcstore.spool import read, spool_path

    records = list(read(spool_path(data_dir=store_dir)))
    run_events = [r for r in records if r.kind == "run_event"]
    assert run_events, "the new run path must still spool run lifecycle events (AC-5.2)"
    # Every run event is attributed to the agent that ran the turn.
    assert all(r.actor_did == agent_did for r in run_events)
    # The turn ran through the streaming entry and recorded its lifecycle.
    names = {r.name for r in run_events}
    assert "turn.end" in names, f"expected turn lifecycle in spool, got {names}"
