"""Context prep on the real turn path: bounded, ordered, visible.

Regression for the 2026-10-04 MC hang: a chat turn ran three unbounded
retrieval passes before its first model call, they stacked past the 120 s
turn-start guard, and the chat failed with nothing in the trace to say why.

Each test drives ``agent.run`` end to end. Only the model wire
(``arcrun.run_stream``) and the retrieval capability are faked; the spool is
captured so the run trace can be read back.
"""

from __future__ import annotations

import asyncio
import logging
import time
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
    SessionConfig,
    TelemetryConfig,
)

from ._signed_documents import sign_workspace_documents


@pytest.fixture()
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "workspace"
    ws.mkdir()
    (ws / "identity.md").write_text("Agent: prep-agent")
    (ws / "context.md").write_text("Open loops: none.")
    sign_workspace_documents(ws, tmp_path / "arcagent.toml")
    return ws


def _config(tmp_path: Path, workspace: Path, **session: Any) -> ArcAgentConfig:
    return ArcAgentConfig(
        agent=AgentConfig(
            name="prep-agent", org="testorg", type="executor", workspace=str(workspace)
        ),
        llm=LLMConfig(model="test/model"),
        identity=IdentityConfig(did="", key_dir=str(tmp_path / "keys"), vault_path=""),
        telemetry=TelemetryConfig(enabled=True),
        context=ContextConfig(max_tokens=100000),
        session=SessionConfig(**session),
    )


class _Wire:
    """The faked model wire and spool: what the provider and the trace would see."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.events: list[Any] = []

    async def run_stream(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append(kwargs)

        async def _gen() -> Any:
            yield TurnEndEvent(final_text="done", tool_calls_made=0)

        return _gen()

    def spool(self, rec: Any, **_: Any) -> None:
        self.events.append(rec)

    def names(self) -> list[str]:
        return [e.name for e in self.events if e.kind == "run_event"]

    def event(self, name: str, index: int = -1) -> Any:
        return [e for e in self.events if e.name == name][index]


def _retrieval(responses: list[dict[str, Any]] | None = None, hold: asyncio.Event | None = None):
    """A ``context_retrieval`` operation: canned candidates per call, or held forever."""
    queue = list(responses or [])

    async def retrieve(query: str, *, memory_top_k: int, docs_top_k: int) -> dict[str, Any]:
        if hold is not None:
            await hold.wait()
        return queue.pop(0) if queue else {"candidates": [], "steps": []}

    async def operation(_agent: Any) -> Any:
        return retrieve

    return operation


def _cand(kind: str, source: str, score: float, text: str, **extra: Any) -> dict[str, Any]:
    return {
        "source_kind": kind,
        "source": source,
        "title": source,
        "score": score,
        "classification": "unclassified",
        "text": text,
        **extra,
    }


async def _run_turns(agent: ArcAgent, wire: _Wire, *messages: str) -> None:
    with (
        patch("arcagent.core.agent_dispatch.arcrun.run_stream", side_effect=wire.run_stream),
        patch("arcagent.core.context_prep.spool_record", side_effect=wire.spool),
    ):
        session = await agent.session("prep-test")
        for message in messages:
            async for _ in agent.run(message, session=session):
                pass


@patch("arcagent.core.model_manager.load_eval_model")
async def test_a_held_retrieval_never_holds_the_turn(
    mock_load_model: MagicMock,
    tmp_path: Path,
    workspace: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    mock_load_model.return_value = MagicMock()
    agent = ArcAgent(
        config=_config(tmp_path, workspace, context_prep_budget_seconds=0.2),
        config_path=tmp_path / "arcagent.toml",
    )
    await agent.startup()
    wire = _Wire()
    held = asyncio.Event()  # never set: the retrieval would wait forever

    started = time.monotonic()
    with (
        patch("arcagent.core.context_prep._retrieval_operation", _retrieval(hold=held)),
        caplog.at_level(logging.WARNING, logger="arcagent.context_prep"),
    ):
        await _run_turns(agent, wire, "what changed on the site?")
    elapsed = time.monotonic() - started

    assert len(wire.calls) == 1, "the model call happened without the retrieval"
    assert elapsed < 5.0
    assert "<agent-context>" not in str(wire.calls[0]["messages"][-1].content)
    skipped = wire.event("context.skipped")
    assert skipped.extra == {"step": "retrieval", "reason": "timed out"}
    assert wire.event("context.retrieval").extra["status"] == "timeout"
    assert any("retrieval timed out" in r.getMessage() for r in caplog.records)


@patch("arcagent.core.model_manager.load_eval_model")
async def test_the_trace_shows_each_pre_model_step_in_request_order(
    mock_load_model: MagicMock, tmp_path: Path, workspace: Path
) -> None:
    mock_load_model.return_value = MagicMock()
    agent = ArcAgent(
        config=_config(
            tmp_path,
            workspace,
            context_memory_top_k=2,
            context_docs_top_k=1,
            context_docs_score_floor=0.1,
        ),
        config_path=tmp_path / "arcagent.toml",
    )
    await agent.startup()
    wire = _Wire()
    found = {
        "candidates": [
            _cand("memory", "m1", 0.9, "seo audit cadence is weekly"),
            _cand("memory", "m2", 0.8, "ahrefs export lives in drive"),
            _cand("memory", "m3", 0.7, "a third memory past top-k"),
            _cand("memory", "m1", 0.6, "seo audit cadence is weekly"),
            _cand("connection", "drive", 0.5, "audit checklist doc", path="docs/audit.md"),
            _cand("connection", "drive", 0.4, "second doc past top-k", path="docs/b.md"),
            _cand("connection", "drive", 0.01, "weak match", path="docs/weak.md"),
        ],
        "steps": [{"name": "memory", "latency_ms": 3.0, "status": "ok"}],
    }
    with patch("arcagent.core.context_prep._retrieval_operation", _retrieval([found])):
        await _run_turns(agent, wire, "set up the weekly seo audit")

    assert wire.names()[:4] == [
        "strategy.selected",
        "context.system",
        "context.retrieval",
        "context.session",
    ]
    retrieval = wire.event("context.retrieval").extra
    assert retrieval["query"] == "set up the weekly seo audit"
    by_source = {(i["source"], i["path"], i["snippet"]): i for i in retrieval["items"]}
    used = [i for i in retrieval["items"] if i["included"]]
    assert [i["source"] for i in used] == ["m1", "m2", "drive"]
    reasons = {i["snippet"]: i["reason"] for i in retrieval["items"] if not i["included"]}
    assert reasons["a third memory past top-k"] == "below top-2"
    assert reasons["second doc past top-k"] == "below top-1"
    assert reasons["weak match"].startswith("score below floor")
    assert "duplicate" in reasons.values()
    assert by_source
    assert retrieval["tokens_injected"] <= retrieval["token_cap"]
    sent = str(wire.calls[0]["messages"][-1].content)
    assert "audit checklist doc" in sent
    assert "a third memory past top-k" not in sent
    assert wire.calls[0]["allowed_strategies"] == [
        wire.event("strategy.selected").extra["strategy"]
    ]


@patch("arcagent.core.model_manager.load_eval_model")
async def test_context_rides_before_the_question_after_a_byte_stable_system_prefix(
    mock_load_model: MagicMock, tmp_path: Path, workspace: Path
) -> None:
    mock_load_model.return_value = MagicMock()
    agent = ArcAgent(config=_config(tmp_path, workspace), config_path=tmp_path / "arcagent.toml")
    await agent.startup()
    wire = _Wire()
    turns = [
        {"candidates": [_cand("memory", "a", 0.9, "first turn memory")], "steps": []},
        {"candidates": [_cand("memory", "b", 0.9, "second turn memory")], "steps": []},
    ]
    with patch("arcagent.core.context_prep._retrieval_operation", _retrieval(turns)):
        await _run_turns(agent, wire, "first question", "second question")

    first, second = wire.calls
    # The cached system prefix does not move a byte though retrieval differs.
    assert first["system_prompt"] == second["system_prompt"]
    assert "turn memory" not in "".join(second["system_prompt"])
    # Wire order: history, then the current turn with <agent-context> FIRST and
    # the person's words last.
    last = str(second["messages"][-1].content)
    assert last.startswith("<agent-context>")
    assert last.endswith("second question")
    assert "second turn memory" in last
    assert second["messages"][: len(first["messages"])] == first["messages"]
    # The trace knows the prefix was reusable on the second turn.
    assert wire.event("context.system", 0).extra["cached"] is False
    assert wire.event("context.system", 1).extra["cached"] is True


@patch("arcagent.core.model_manager.load_eval_model")
async def test_the_strategy_is_picked_on_the_configured_small_model_and_pins_the_run(
    mock_load_model: MagicMock, tmp_path: Path, workspace: Path
) -> None:
    from arcllm import LLMResponse, ToolCall, Usage

    from arcagent.core.config import ArcRunConfig

    small = MagicMock(close=AsyncMock())
    small.invoke = AsyncMock(
        return_value=LLMResponse(
            tool_calls=[
                ToolCall(
                    id="s",
                    name="select_strategy",
                    arguments={"strategy": "code", "reasoning": "needs a script"},
                )
            ],
            stop_reason="tool_use",
            model="test/small",
            usage=Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        )
    )
    main = MagicMock(close=AsyncMock())
    mock_load_model.side_effect = lambda name, **_: small if name == "test/small" else main
    config = _config(tmp_path, workspace)
    config = config.model_copy(update={"arcrun": ArcRunConfig(strategy_model="test/small")})
    agent = ArcAgent(config=config, config_path=tmp_path / "arcagent.toml")
    await agent.startup()
    wire = _Wire()
    with patch("arcagent.core.context_prep._retrieval_operation", _retrieval()):
        await _run_turns(agent, wire, "crunch this csv")

    chosen = wire.event("strategy.selected").extra
    assert (chosen["strategy"], chosen["selected_by"]) == ("code", "model")
    assert chosen["reason"] == "needs a script"
    assert small.invoke.await_count == 1
    assert wire.calls[0]["allowed_strategies"] == ["code"]
    assert wire.calls[0]["model"] is main
    await agent.shutdown()
    small.close.assert_awaited()


@patch("arcagent.core.model_manager.load_eval_model")
async def test_a_turn_that_never_spawns_assembles_its_prompt_once(
    mock_load_model: MagicMock, tmp_path: Path, workspace: Path
) -> None:
    mock_load_model.return_value = MagicMock()
    agent = ArcAgent(config=_config(tmp_path, workspace), config_path=tmp_path / "arcagent.toml")
    await agent.startup()
    assert agent._config.spawn.enabled
    assert agent._context is not None
    real = agent._context.assemble_system_prompt
    assemblies: list[dict[str, Any]] = []

    async def counting(*args: Any, **kwargs: Any) -> Any:
        assemblies.append(kwargs)
        return await real(*args, **kwargs)

    wire = _Wire()
    with (
        patch.object(agent._context, "assemble_system_prompt", counting),
        patch("arcagent.core.context_prep._retrieval_operation", _retrieval()),
    ):
        await _run_turns(agent, wire, "hello")

    assert len(assemblies) == 1, "the spawn child prompt is built only when a child spawns"
    assert len(wire.calls) == 1


@patch("arcagent.core.model_manager.load_eval_model")
async def test_a_turn_that_fails_before_its_run_is_closed_on_the_trace(
    mock_load_model: MagicMock, tmp_path: Path, workspace: Path
) -> None:
    mock_load_model.return_value = MagicMock()
    agent = ArcAgent(config=_config(tmp_path, workspace), config_path=tmp_path / "arcagent.toml")
    await agent.startup()
    wire = _Wire()

    async def broken_assembly(*_: Any, **__: Any) -> Any:
        raise RuntimeError("assembly exploded")

    assert agent._context is not None
    with (
        patch.object(agent._context, "assemble_system_prompt", broken_assembly),
        pytest.raises(RuntimeError),
    ):
        await _run_turns(agent, wire, "hello")

    closed = wire.event("run.not_started")
    assert closed.outcome == "failed"
    assert "RuntimeError" in closed.extra["reason"]
    assert wire.calls == []
