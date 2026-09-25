"""An unconfirmed tool result pauses the run before any model retry or next effect."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from packages.arcrun.tests.conftest import LLMResponse, ToolCall

import arcrun
from arcrun.ledger import ToolExecutionIntent, ToolExecutionOutcome, ToolLedgerEntry
from arcrun.types import Tool


class _LostCompletion:
    def __init__(self, *, unresolved: bool = False) -> None:
        self.unresolved = unresolved
        self.intents: list[ToolExecutionIntent] = []
        self.committed: list[ToolExecutionOutcome] = []

    async def begin(self, intent: ToolExecutionIntent) -> ToolLedgerEntry:
        self.intents.append(intent)
        return ToolLedgerEntry(status="unresolved" if self.unresolved else "new")

    async def complete(self, _outcome: ToolExecutionOutcome) -> None:
        self.committed.append(_outcome)
        raise ConnectionError("private ledger endpoint and result")


class _DurableCompletion(_LostCompletion):
    async def complete(self, _outcome: ToolExecutionOutcome) -> None:
        return None


class _RetryModel:
    def __init__(self, first_calls: list[ToolCall]) -> None:
        self.calls = 0
        self.first_calls = first_calls

    async def invoke(self, _messages: Any, **_kwargs: Any) -> LLMResponse:
        self.calls += 1
        return LLMResponse(
            tool_calls=self.first_calls
            if self.calls == 1
            else [ToolCall(id="new-id-retry", name="write", arguments={})],
            stop_reason="tool_use",
        )


def _tool(effects: list[str]) -> Tool:
    async def execute(_args: dict[str, Any], _ctx: Any) -> str:
        effects.append("write")
        return "private result"

    return Tool(name="write", description="write", input_schema={"type": "object"}, execute=execute)


@pytest.mark.asyncio
async def test_lost_completion_halts_before_new_call_id_retry() -> None:
    effects: list[str] = []
    model = _RetryModel([ToolCall(id="first", name="write", arguments={})])
    ledger = _LostCompletion()
    result = await arcrun.run(
        model,
        arcrun.StaticProvider([_tool(effects)]),
        "system",
        "write once",
        allowed_strategies=["react"],
        run_id="uncertain-run",
        tool_ledger=ledger,
    )
    assert effects == ["write"]
    assert model.calls == 1
    assert len(ledger.committed) == 1
    assert result.outcome_unknown is not None
    assert result.outcome_unknown.tool_call_id == "first"
    assert result.completion_payload is not None
    assert result.completion_payload["error"] == "tool_outcome_unknown"
    assert [event.type for event in result.events].count("loop.outcome_unknown") == 1
    assert [event.type for event in result.events].count("loop.complete") == 1
    assert "private" not in str(result.completion_payload)
    assert "private ledger endpoint" not in str(result.events)
    assert "private result" not in str(result.events)
    assert result.tool_calls_made == 1


@pytest.mark.asyncio
async def test_lost_completion_blocks_remaining_calls_in_same_batch() -> None:
    effects: list[str] = []
    model = _RetryModel(
        [
            ToolCall(id="first", name="write", arguments={}),
            ToolCall(id="second", name="write", arguments={}),
        ]
    )
    result = await arcrun.run(
        model,
        arcrun.StaticProvider([_tool(effects)]),
        "system",
        "write once",
        allowed_strategies=["react"],
        tool_ledger=_LostCompletion(),
    )
    assert effects == ["write"]
    assert model.calls == 1
    assert result.outcome_unknown is not None


@pytest.mark.asyncio
async def test_unresolved_prior_intent_halts_without_reexecution() -> None:
    effects: list[str] = []
    result = await arcrun.run(
        _RetryModel([ToolCall(id="first", name="write", arguments={})]),
        arcrun.StaticProvider([_tool(effects)]),
        "system",
        "write once",
        allowed_strategies=["react"],
        tool_ledger=_LostCompletion(unresolved=True),
    )
    assert effects == []
    assert result.outcome_unknown is not None
    assert result.outcome_unknown.phase == "reconciliation_required"


@pytest.mark.asyncio
async def test_stream_collect_preserves_typed_unknown_terminal() -> None:
    effects: list[str] = []
    stream = await arcrun.run_stream(
        model=_RetryModel([ToolCall(id="first", name="write", arguments={})]),
        capabilities=arcrun.StaticProvider([_tool(effects)]),
        system_prompt="system",
        task="write once",
        allowed_strategies=["react"],
        tool_ledger=_LostCompletion(),
    )
    result = await arcrun.collect(stream)
    assert effects == ["write"]
    assert result.outcome_unknown is not None
    assert result.outcome_unknown.tool_call_id == "first"


@pytest.mark.asyncio
async def test_tool_timeout_after_side_effect_pauses_before_retry() -> None:
    effects: list[str] = []

    async def execute(_args: dict[str, Any], _ctx: Any) -> str:
        effects.append("write")
        await asyncio.Event().wait()
        return "done"

    model = _RetryModel([ToolCall(id="first", name="write", arguments={})])
    tool = Tool(
        name="write",
        description="write",
        input_schema={"type": "object"},
        execute=execute,
        timeout_seconds=0.01,
    )
    result = await arcrun.run(
        model,
        arcrun.StaticProvider([tool]),
        "system",
        "write once",
        allowed_strategies=["react"],
        tool_ledger=_DurableCompletion(),
    )
    assert effects == ["write"]
    assert model.calls == 1
    assert result.outcome_unknown is not None
    assert result.outcome_unknown.phase == "execution_unconfirmed"


@pytest.mark.asyncio
async def test_operator_cancel_after_side_effect_preserves_unknown_terminal() -> None:
    effects: list[str] = []
    started = asyncio.Event()

    async def execute(_args: dict[str, Any], _ctx: Any) -> str:
        effects.append("write")
        started.set()
        await asyncio.Event().wait()
        return "done"

    model = _RetryModel([ToolCall(id="first", name="write", arguments={})])
    tool = Tool(name="write", description="write", input_schema={"type": "object"}, execute=execute)
    handle = await arcrun.run_async(
        model,
        arcrun.StaticProvider([tool]),
        "system",
        "write once",
        allowed_strategies=["react"],
        tool_ledger=_DurableCompletion(),
    )
    await asyncio.wait_for(started.wait(), 1)
    await handle.cancel("did:arc:operator", "stop")
    result = await handle.result()
    assert effects == ["write"]
    assert model.calls == 1
    assert result.outcome_unknown is not None
    assert result.outcome_unknown.phase == "execution_unconfirmed"


@pytest.mark.asyncio
async def test_dynamic_child_unknown_blocks_later_child_effects() -> None:
    effects: list[str] = []
    script = (
        'agent("first", {"capability_mode": "all"})\n'
        'agent("second", {"capability_mode": "all"})\n'
        'complete("done")\n'
    )

    class Model:
        def __init__(self) -> None:
            self.child_calls = 0

        async def invoke(
            self, _messages: Any, tools: list[Any] | None = None, **_kwargs: Any
        ) -> LLMResponse:
            if any(getattr(tool, "name", "") == "emit_script" for tool in tools or []):
                return LLMResponse(
                    tool_calls=[
                        ToolCall(id="script", name="emit_script", arguments={"source": script})
                    ],
                    stop_reason="tool_use",
                )
            self.child_calls += 1
            return LLMResponse(
                tool_calls=[ToolCall(id=f"write-{self.child_calls}", name="write", arguments={})],
                stop_reason="tool_use",
            )

    model = Model()
    result = await arcrun.run(
        model,
        arcrun.StaticProvider([_tool(effects)]),
        "system",
        "write once",
        allowed_strategies=["dynamic"],
        tool_ledger=_LostCompletion(),
    )
    assert effects == ["write"]
    assert model.child_calls == 1
    assert result.outcome_unknown is not None
    assert result.completion_payload is not None
    assert result.completion_payload["error"] == "tool_outcome_unknown"
    assert [event.type for event in result.events].count("loop.outcome_unknown") == 1
    assert [event.type for event in result.events].count("loop.complete") == 1
    assert [event.type for event in result.events].count("dynamic.agent.start") == 1
    child_end = [event for event in result.events if event.type == "dynamic.agent.end"]
    assert len(child_end) == 1
    assert child_end[0].data["success"] is False


@pytest.mark.asyncio
async def test_parallel_inflight_sibling_is_joined_and_reported_before_unknown_terminal() -> None:
    read_started = asyncio.Event()
    release_read = asyncio.Event()
    completion_lost = asyncio.Event()
    effects: list[str] = []

    class Ledger(_DurableCompletion):
        def __init__(self) -> None:
            super().__init__()
            self.call_ids: dict[str, str] = {}

        async def begin(self, intent: ToolExecutionIntent) -> ToolLedgerEntry:
            self.call_ids[intent.invocation_key] = intent.tool_call_id
            return await super().begin(intent)

        async def complete(self, outcome: ToolExecutionOutcome) -> None:
            if self.call_ids[outcome.invocation_key] == "first":
                await read_started.wait()
                completion_lost.set()
                raise ConnectionError("lost completion response")

    async def write(_args: dict[str, Any], _ctx: Any) -> str:
        effects.append("write")
        return "written"

    async def read(_args: dict[str, Any], _ctx: Any) -> str:
        effects.append("read-start")
        read_started.set()
        await release_read.wait()
        effects.append("read-end")
        return "read"

    tools = [
        Tool(
            name="write",
            description="write",
            input_schema={"type": "object"},
            execute=write,
            classification="read_only",
        ),
        Tool(
            name="read",
            description="read",
            input_schema={"type": "object"},
            execute=read,
            classification="read_only",
        ),
    ]
    model = _RetryModel(
        [
            ToolCall(id="first", name="write", arguments={}),
            ToolCall(id="sibling", name="read", arguments={}),
        ]
    )
    running = asyncio.create_task(
        arcrun.run(
            model,
            arcrun.StaticProvider(tools),
            "system",
            "batch",
            allowed_strategies=["react"],
            tool_ledger=Ledger(),
        )
    )
    await asyncio.wait_for(completion_lost.wait(), 1)
    assert not running.done()
    release_read.set()
    result = await asyncio.wait_for(running, 1)
    assert effects == ["write", "read-start", "read-end"]
    assert model.calls == 1
    assert result.outcome_unknown is not None
    assert result.tool_calls_made == 2
    event_types = [event.type for event in result.events]
    assert event_types.index("tool.end") < event_types.index("loop.outcome_unknown")
