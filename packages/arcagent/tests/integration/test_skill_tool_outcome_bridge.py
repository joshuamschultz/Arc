"""Real ArcRun executor → ArcAgent tool wrapper → skill improver outcome path."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arcllm.trace_store import TraceRecord
from arcrun import current_run_id, run_oneshot
from arcrun.events import EventBus
from arcrun.executor import execute_tool_call
from arcrun.ledger import ToolExecutionIntent, ToolExecutionOutcome
from arcrun.registry import ToolRegistry as RunToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.types import Tool
from arcskill.improver import ArcSkillImprover, ImproverConfig

from arcagent.core.config import ToolsConfig
from arcagent.core.model_manager import create_arcllm_bridge, create_arcrun_bridge
from arcagent.core.module_bus import EventContext, ModuleBus
from arcagent.core.session_internal.capability_ledger import bind_session_id, reset_session_id
from arcagent.core.tool_registry import RegisteredTool, ToolRegistry, ToolTransport
from arcagent.modules.skills import _runtime
from arcagent.modules.skills.capabilities import (
    skills_llm_call_complete,
    skills_post_plan,
    skills_post_tool,
    skills_pre_plan,
)


@dataclass
class _ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class _LedgerEntry:
    status: str
    outcome: ToolExecutionOutcome | None = None


class _Ledger:
    def __init__(self) -> None:
        self.entries: dict[str, _LedgerEntry] = {}

    async def begin(self, intent: ToolExecutionIntent) -> _LedgerEntry:
        return self.entries.setdefault(intent.invocation_key, _LedgerEntry("new"))

    async def complete(self, outcome: ToolExecutionOutcome) -> None:
        self.entries[outcome.invocation_key] = _LedgerEntry("completed", outcome)


class _FailingCompletionLedger(_Ledger):
    async def complete(self, outcome: ToolExecutionOutcome) -> None:
        raise RuntimeError("ledger unavailable after execution")


@pytest.fixture(autouse=True)
def _clean_skill_state() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _telemetry() -> MagicMock:
    telemetry = MagicMock()
    span = MagicMock()
    span.__aenter__ = AsyncMock()
    span.__aexit__ = AsyncMock(return_value=False)
    telemetry.tool_span.return_value = span
    return telemetry


async def _settle_bridge() -> None:
    for _ in range(24):
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_llm_and_turn_bridges_stamp_live_run_and_session_identity() -> None:
    bus = ModuleBus()
    seen: list[tuple[str, dict[str, Any]]] = []

    async def capture(ctx: EventContext) -> None:
        seen.append((ctx.event, ctx.data))

    bus.subscribe("llm:call_complete", capture, module_name="test")
    bus.subscribe("agent:post_plan", capture, module_name="test")
    llm_bridge = create_arcllm_bridge(bus)
    run_bridge = create_arcrun_bridge(bus, session_id="session-a", agent_did="did:arc:a")
    llm_bridge(
        TraceRecord(
            provider="test", model="test", lineage={"session_id": "session-a", "run_id": "run-a"}
        )
    )
    EventBus(run_id="run-a", on_event=run_bridge).emit("turn.end", {"turn_number": 1})
    await _settle_bridge()

    assert len(seen) == 2
    assert all(data["session_id"] == "session-a" for _, data in seen)
    assert all(data["run_id"] == "run-a" for _, data in seen)
    assert seen[1][1]["agent_did"] == "did:arc:a"


@pytest.mark.asyncio
async def test_llm_bridge_preserves_canonical_record_ids_over_context() -> None:
    bus = ModuleBus()
    seen: list[dict[str, Any]] = []

    async def capture(ctx: EventContext) -> None:
        seen.append(ctx.data)

    bus.subscribe("llm:call_complete", capture, module_name="test")
    bridge = create_arcllm_bridge(bus)
    token = bind_session_id("ambient-session")
    try:
        bridge(
            {
                "event_type": "llm_call",
                "session_id": "record-session",
                "run_id": "record-run",
                "lineage": {"session_id": "lineage-session", "run_id": "lineage-run"},
            }
        )
    finally:
        reset_session_id(token)
    await bus.flush_ordered("record-run")
    assert seen[0]["session_id"] == "record-session"
    assert seen[0]["run_id"] == "record-run"


@pytest.mark.asyncio
async def test_nested_personal_oneshot_callback_uses_its_own_run_id() -> None:
    bus = ModuleBus()
    seen: list[dict[str, Any]] = []

    async def capture(ctx: EventContext) -> None:
        seen.append(ctx.data)

    bus.subscribe("llm:call_complete", capture, module_name="test")
    bridge = create_arcllm_bridge(bus)

    class Model:
        async def invoke(self, _messages: Any, **_kwargs: Any) -> Any:
            bridge(TraceRecord(provider="test", model="test"))
            return MagicMock(content="done", usage=None)

    class OuterModel:
        async def invoke(self, _messages: Any, **_kwargs: Any) -> Any:
            assert current_run_id() == "outer-run"
            await run_oneshot(Model(), user="Evaluate", run_id="evaluation-run")
            assert current_run_id() == "outer-run"
            return MagicMock(content="outer done", usage=None)

    await run_oneshot(OuterModel(), user="Run", run_id="outer-run")
    await bus.flush_ordered("evaluation-run")
    assert [record["run_id"] for record in seen] == ["evaluation-run"]
    assert current_run_id() is None


@pytest.mark.asyncio
async def test_blocked_prior_tool_outcome_finishes_before_next_turn_opens(tmp_path: Path) -> None:
    skill = tmp_path / "SKILL.md"
    skill.write_text("# helper\n", encoding="utf-8")
    adapter = MagicMock()
    adapter.observe = AsyncMock()
    adapter.on_turn_end = AsyncMock()
    state = _runtime._State(adapter=adapter, active=True, workspace=tmp_path)
    state.skill_paths = {skill.resolve(): "helper"}
    _runtime.bind(state)
    bus = ModuleBus()
    blocked = asyncio.Event()
    release = asyncio.Event()

    async def hold_prior_work(ctx: EventContext) -> None:
        if ctx.data.get("call_id") == "work-1":
            blocked.set()
            await release.wait()

    bus.subscribe("agent:pre_plan", skills_pre_plan, module_name="skills")
    bus.subscribe("agent:post_tool", hold_prior_work, priority=100, module_name="test")
    bus.subscribe("agent:post_tool", skills_post_tool, priority=200, module_name="skills")
    bus.subscribe("agent:post_plan", skills_post_plan, module_name="skills")
    staged = {
        "read-1": {"args": {"file_path": str(skill)}, "result": "safe", "duration": 0.1},
        "read-2": {"args": {"file_path": str(skill)}, "result": "safe", "duration": 0.1},
        "work-1": {"args": {}, "result": "safe", "duration": 0.1},
        "work-2": {"args": {}, "result": "safe", "duration": 0.1},
    }

    def take(_run_id: str, call_id: str) -> dict[str, Any] | None:
        return staged.pop(call_id, None)

    producer = EventBus(
        run_id="same-run",
        on_event=create_arcrun_bridge(bus, take_tool_outcome=take, session_id="same-session"),
    )
    producer.emit("turn.start", {"turn_number": 1})
    producer.emit("tool.end", {"name": "read", "tool_call_id": "read-1", "turn_number": 1})
    producer.emit("tool.end", {"name": "work", "tool_call_id": "work-1", "turn_number": 1})
    await asyncio.wait_for(blocked.wait(), 1)
    producer.emit("turn.end", {"turn_number": 1})
    producer.emit("turn.start", {"turn_number": 2})
    producer.emit("tool.end", {"name": "read", "tool_call_id": "read-2", "turn_number": 2})
    producer.emit("tool.end", {"name": "work", "tool_call_id": "work-2", "turn_number": 2})
    producer.emit("turn.end", {"turn_number": 2})
    await asyncio.sleep(0)
    assert state.turn("same-session", "same-run").turn_number == 1
    assert adapter.observe.await_count == 0
    release.set()
    await bus.flush_ordered("same-run")
    assert adapter.observe.await_count == 2
    assert [call.kwargs["call_id"] for call in adapter.observe.await_args_list] == [
        "work-1",
        "work-2",
    ]
    assert adapter.on_turn_end.await_count == 2


@pytest.mark.asyncio
async def test_arcrun_terminal_events_retain_exact_call_id_for_same_name_tools() -> None:
    events: list[Any] = []
    event_bus = EventBus(run_id="run-concurrent", on_event=events.append)

    async def execute(args: dict[str, Any], _ctx: Any) -> str:
        if args["fail"]:
            raise ValueError("private message")
        return "done"

    tool = Tool(
        name="work",
        description="work",
        input_schema={"type": "object", "properties": {"fail": {"type": "boolean"}}},
        execute=execute,
    )
    state = RunState(
        messages=[],
        registry=RunToolRegistry(tools=[tool], event_bus=event_bus),
        event_bus=event_bus,
        run_id="run-concurrent",
    )
    sandbox = Sandbox(config=None, event_bus=event_bus)
    await asyncio.gather(
        execute_tool_call(_ToolCall("call-fail", "work", {"fail": True}), state, sandbox),
        execute_tool_call(_ToolCall("call-ok", "work", {"fail": False}), state, sandbox),
    )

    terminal = [event for event in events if event.type in {"tool.end", "tool.error"}]
    assert {(event.type, event.data["tool_call_id"]) for event in terminal} == {
        ("tool.error", "call-fail"),
        ("tool.end", "call-ok"),
    }


@pytest.mark.asyncio
async def test_late_ledger_failure_emits_one_failure_and_discards_staged_result(
    tmp_path: Path,
) -> None:
    adapter = MagicMock()
    adapter.observe = AsyncMock()
    skill_state = _runtime._State(adapter=adapter, active=True, workspace=tmp_path)
    skill_state.turn("session", "run-ledger").active_skill = "helper"
    _runtime.bind(skill_state)
    bus = ModuleBus()
    seen: list[EventContext] = []

    async def record(ctx: EventContext) -> None:
        seen.append(ctx)

    bus.subscribe("agent:post_tool", skills_post_tool, module_name="skills")
    bus.subscribe("agent:post_tool", record, module_name="test")
    registry = ToolRegistry(config=ToolsConfig(), bus=bus, telemetry=_telemetry())

    async def execute(**_kwargs: Any) -> str:
        return "sensitive tool result"

    registry.register(
        RegisteredTool(
            name="work",
            description="work",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=execute,
        )
    )
    event_bus = EventBus(
        run_id="run-ledger",
        on_event=create_arcrun_bridge(
            bus,
            bridge_only_tools=frozenset(),
            take_tool_outcome=registry.take_tool_outcome,
            session_id="session",
        ),
    )
    state = RunState(
        messages=[],
        registry=RunToolRegistry(tools=registry.to_arcrun_tools(), event_bus=event_bus),
        event_bus=event_bus,
        run_id="run-ledger",
        tool_ledger=_FailingCompletionLedger(),
    )
    token = bind_session_id("session")
    _message, success = await execute_tool_call(
        _ToolCall("call-ledger", "work", {}),
        state,
        Sandbox(config=None, event_bus=event_bus),
    )
    reset_session_id(token)
    await _settle_bridge()

    assert not success
    assert [(ctx.data["status"], ctx.data["call_id"]) for ctx in seen] == [
        ("error", "call-ledger")
    ]
    assert "result" not in seen[0].data
    assert registry.take_tool_outcome("run-ledger", "call-ledger") is None
    adapter.observe.assert_awaited_once()
    assert adapter.observe.await_args.kwargs["status"] == "error"


@pytest.mark.asyncio
async def test_interleaved_sessions_keep_skill_and_llm_trace_attribution(tmp_path: Path) -> None:
    skills = {}
    for name in ("alpha", "beta"):
        skill = tmp_path / "skills" / name / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(f"# {name}\n", encoding="utf-8")
        skills[skill.resolve()] = name
    improver = ArcSkillImprover(
        tmp_path / "workspace",
        config=ImproverConfig(optimize_after_uses=999),
        tier="personal",
    )
    state = _runtime._State(adapter=improver, active=True, workspace=tmp_path)
    state.skill_paths = skills
    _runtime.bind(state)
    bus = ModuleBus()
    bus.subscribe("agent:post_tool", skills_post_tool, module_name="skills")
    bus.subscribe("agent:post_plan", skills_post_plan, module_name="skills")
    bus.subscribe("llm:call_complete", skills_llm_call_complete, module_name="skills")

    for session, run, skill, trace in (
        ("session-a", "run-a", "alpha", "trace-a"),
        ("session-b", "run-b", "beta", "trace-b"),
    ):
        path = next(path for path, name in skills.items() if name == skill)
        await bus.emit(
            "agent:post_tool",
            {
                "tool": "read",
                "status": "ok",
                "args": {"file_path": str(path)},
                "session_id": session,
                "run_id": run,
                "call_id": f"read-{run}",
            },
        )
        await bus.emit(
            "llm:call_complete",
            {
                "trace_id": trace,
                "session_id": session,
                "run_id": run,
            },
        )

    for session, run in (("session-a", "run-a"), ("session-b", "run-b")):
        outcome = {
            "tool": "work",
            "status": "error" if session == "session-b" else "ok",
            "session_id": session,
            "run_id": run,
            "call_id": f"work-{run}",
        }
        await bus.emit("agent:post_tool", outcome)
        await bus.emit("agent:post_tool", outcome)  # reconnect redelivery of the same call
        if session == "session-b":
            assert state.turn(session, run).error_counts == {"beta": 1}
        await bus.emit(
            "agent:post_plan",
            {
                "turn_number": 1,
                "task_outcome": "success",
                "session_id": session,
                "run_id": run,
            },
        )
        await bus.emit(
            "agent:post_plan",
            {
                "turn_number": 1,
                "task_outcome": "success",
                "session_id": session,
                "run_id": run,
            },
        )

    for session, skill, trace in (
        ("session-a", "alpha", "trace-a"),
        ("session-b", "beta", "trace-b"),
    ):
        spans = improver._store.load_traces(skill)
        assert len(spans) == 1
        assert spans[0].session_id == session
        assert spans[0].llm_trace_ids == [trace]
        assert len(spans[0].tool_calls) == 1
        assert spans[0].tool_calls[0].result_status == (
            "error" if session == "session-b" else "ok"
        )
    assert improver._store.usage_counts == {"alpha": 1, "beta": 1}
    await improver.aclose()


@pytest.mark.asyncio
async def test_second_turn_in_same_run_reopens_without_replaying_prior_close(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skills" / "helper" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# helper\n", encoding="utf-8")
    improver = ArcSkillImprover(
        tmp_path / "workspace",
        config=ImproverConfig(optimize_after_uses=999),
        tier="personal",
    )
    state = _runtime._State(adapter=improver, active=True, workspace=tmp_path)
    state.skill_paths = {skill.resolve(): "helper"}
    _runtime.bind(state)
    bus = ModuleBus()
    bus.subscribe("agent:pre_plan", skills_pre_plan, module_name="skills")
    bus.subscribe("agent:post_tool", skills_post_tool, module_name="skills")
    bus.subscribe("agent:post_plan", skills_post_plan, module_name="skills")
    scope = {"session_id": "session", "run_id": "run"}

    for turn in (1, 2):
        await bus.emit("agent:pre_plan", {**scope, "turn_number": turn})
        await bus.emit(
            "agent:post_tool",
            {
                **scope,
                "tool": "read",
                "status": "ok",
                "args": {"file_path": str(skill)},
                "call_id": f"read-{turn}",
            },
        )
        if turn == 2:
            await bus.emit(
                "agent:post_tool",
                {**scope, "tool": "work", "status": "ok", "call_id": "work-1"},
            )
        await bus.emit(
            "agent:post_tool",
            {
                **scope,
                "tool": "work",
                "status": "ok",
                "call_id": f"work-{turn}",
            },
        )
        await bus.emit("agent:post_plan", {**scope, "turn_number": turn})
        if turn == 1:
            await bus.emit("agent:pre_plan", {**scope, "turn_number": 2})
            await bus.emit("agent:post_plan", {**scope, "turn_number": 1})

    spans = improver._store.load_traces("helper")
    assert len(spans) == 2
    assert [len(span.tool_calls) for span in spans] == [1, 1]
    await improver.aclose()


@pytest.mark.asyncio
async def test_registered_tool_success_failure_and_secret_args_reach_one_skill_span(
    tmp_path: Path,
) -> None:
    skill = tmp_path / "skills" / "helper" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("# helper\n", encoding="utf-8")
    improver = ArcSkillImprover(
        tmp_path / "workspace",
        config=ImproverConfig(optimize_after_uses=999, capture_args=True),
        tier="personal",
        skill_path=lambda _name: skill,
    )
    skill_state = _runtime._State(adapter=improver, active=True, workspace=tmp_path)
    skill_state.skill_paths = {skill.resolve(): "helper"}
    _runtime.bind(skill_state)

    module_bus = ModuleBus()
    seen: list[EventContext] = []

    async def record(ctx: EventContext) -> None:
        seen.append(ctx)

    module_bus.subscribe("agent:post_tool", skills_post_tool, module_name="skills")
    module_bus.subscribe("agent:post_tool", record, module_name="test")
    module_bus.subscribe("agent:post_plan", skills_post_plan, module_name="skills")
    registry = ToolRegistry(
        config=ToolsConfig(),
        bus=module_bus,
        telemetry=_telemetry(),
        agent_did="did:arc:test:agent",
    )

    async def read(**_kwargs: Any) -> str:
        return "# helper"

    async def work(**kwargs: Any) -> str:
        if kwargs.get("fail"):
            raise RuntimeError("private failure text")
        return "worked"

    for name, execute in (("read", read), ("work", work)):
        registry.register(
            RegisteredTool(
                name=name,
                description=name,
                input_schema={"type": "object"},
                transport=ToolTransport.NATIVE,
                execute=execute,
            )
        )
    run_id = "run-skill-1"
    bridge = create_arcrun_bridge(
        module_bus,
        bridge_only_tools=frozenset(),
        take_tool_outcome=registry.take_tool_outcome,
        session_id="session-1",
        agent_did="did:arc:test:agent",
    )
    event_bus = EventBus(run_id=run_id, on_event=bridge)
    state = RunState(
        messages=[],
        registry=RunToolRegistry(tools=registry.to_arcrun_tools(), event_bus=event_bus),
        event_bus=event_bus,
        run_id=run_id,
    )
    sandbox = Sandbox(config=None, event_bus=event_bus)

    token = bind_session_id("session-1")
    for call in (
        _ToolCall("call-read", "read", {"file_path": str(skill)}),
        _ToolCall("call-ok", "work", {"input": "normal"}),
        _ToolCall("call-secret", "work", {"password": "hunter2", "input": "sk-abcdefghijklmnop"}),
        _ToolCall("call-fail", "work", {"fail": True}),
    ):
        await execute_tool_call(call, state, sandbox)
        await _settle_bridge()
    reset_session_id(token)

    await module_bus.emit(
        "agent:post_plan",
        {
            "turn_number": 1,
            "task_outcome": "failure",
            "session_id": "session-1",
            "run_id": run_id,
        },
    )
    traces = improver._store.load_traces("helper")

    assert len(traces) == 1
    assert [call.result_status for call in traces[0].tool_calls] == ["ok", "ok", "error"]
    assert traces[0].tool_calls[0].args == {"input": "normal"}
    assert traces[0].tool_calls[1].args is None
    assert "hunter2" not in str(traces[0].to_dict())
    assert "private failure text" not in str(traces[0].to_dict())
    assert "hunter2" not in str([ctx.data for ctx in seen])
    assert "sk-abcdefghijklmnopqrstuvwxyz123456" not in str([ctx.data for ctx in seen])
    assert [(ctx.data["tool"], ctx.data["status"]) for ctx in seen] == [
        ("read", "ok"),
        ("work", "ok"),
        ("work", "ok"),
        ("work", "error"),
    ]
    assert all(ctx.agent_did == "did:arc:test:agent" for ctx in seen)
    assert all(ctx.data["run_id"] == run_id for ctx in seen)
    assert seen[1].data["call_id"] == "call-ok"
    assert seen[-1].data["session_id"] == "session-1"
    await improver.aclose()


@pytest.mark.asyncio
async def test_replayed_cancelled_and_malformed_run_events_cannot_observe_success(
    tmp_path: Path,
) -> None:
    adapter = MagicMock()
    adapter.observe = AsyncMock()
    skill_state = _runtime._State(adapter=adapter, active=True, workspace=tmp_path)
    skill_state.turn("", "run-replay").active_skill = "helper"
    _runtime.bind(skill_state)
    module_bus = ModuleBus()
    module_bus.subscribe("agent:post_tool", skills_post_tool, module_name="skills")
    event_bus = EventBus(
        run_id="run-replay",
        on_event=create_arcrun_bridge(module_bus, bridge_only_tools=frozenset()),
    )

    event_bus.emit("tool.end", {"name": "work", "replayed": True})
    event_bus.emit("tool.error", {"name": "work", "error": "RunWorkCancelledError"})
    event_bus.emit("tool.end", {"name": ""})
    await _settle_bridge()

    adapter.observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_dynamic_tool_replacement_keeps_one_observation_per_completed_call(
    tmp_path: Path,
) -> None:
    adapter = MagicMock()
    adapter.observe = AsyncMock()
    skill_state = _runtime._State(adapter=adapter, active=True, workspace=tmp_path)
    for name in ("first", "second"):
        skill_state.turn("", f"run-{name}").active_skill = "helper"
    _runtime.bind(skill_state)
    module_bus = ModuleBus()
    module_bus.subscribe("agent:post_tool", skills_post_tool, module_name="skills")
    registry = ToolRegistry(config=ToolsConfig(), bus=module_bus, telemetry=_telemetry())
    bridge = create_arcrun_bridge(module_bus, bridge_only_tools=frozenset())

    async def execute(**_kwargs: Any) -> str:
        return "done"

    for name in ("first", "second"):
        registry.register(
            RegisteredTool(
                name=name,
                description=name,
                input_schema={"type": "object"},
                transport=ToolTransport.NATIVE,
                execute=execute,
            )
        )
        event_bus = EventBus(run_id=f"run-{name}", on_event=bridge)
        state = RunState(
            messages=[],
            registry=RunToolRegistry(tools=registry.to_arcrun_tools(), event_bus=event_bus),
            event_bus=event_bus,
            run_id=f"run-{name}",
        )
        await execute_tool_call(
            _ToolCall(f"call-{name}", name, {}),
            state,
            Sandbox(config=None, event_bus=event_bus),
        )
        await _settle_bridge()
        assert registry.unregister(name)

    assert adapter.observe.await_count == 2
    assert [call.kwargs["tool_name"] for call in adapter.observe.await_args_list] == [
        "first",
        "second",
    ]


@pytest.mark.asyncio
async def test_real_run_cancellation_is_not_counted_as_skill_success(tmp_path: Path) -> None:
    adapter = MagicMock()
    adapter.observe = AsyncMock()
    skill_state = _runtime._State(adapter=adapter, active=True, workspace=tmp_path)
    skill_state.turn("", "run-cancel").active_skill = "helper"
    _runtime.bind(skill_state)
    module_bus = ModuleBus()
    observed: list[tuple[str, str]] = []

    async def record(ctx: EventContext) -> None:
        observed.append((str(ctx.data["status"]), str(ctx.data["call_id"])))

    module_bus.subscribe("agent:post_tool", skills_post_tool, module_name="skills")
    module_bus.subscribe("agent:post_tool", record, module_name="test")
    registry = ToolRegistry(config=ToolsConfig(), bus=module_bus, telemetry=_telemetry())

    async def slow(**_kwargs: Any) -> str:
        await asyncio.sleep(10)
        return "should never finish"

    registry.register(
        RegisteredTool(
            name="slow",
            description="slow",
            input_schema={"type": "object"},
            transport=ToolTransport.NATIVE,
            execute=slow,
        )
    )
    event_bus = EventBus(
        run_id="run-cancel",
        on_event=create_arcrun_bridge(module_bus, bridge_only_tools=frozenset()),
    )
    state = RunState(
        messages=[],
        registry=RunToolRegistry(tools=registry.to_arcrun_tools(), event_bus=event_bus),
        event_bus=event_bus,
        run_id="run-cancel",
    )
    task = asyncio.create_task(
        execute_tool_call(
            _ToolCall("call-cancel", "slow", {}),
            state,
            Sandbox(config=None, event_bus=event_bus),
        )
    )
    await asyncio.sleep(0.01)
    state.cancel_event.set()
    _message, success = await task
    await _settle_bridge()

    assert not success
    assert observed == [("cancelled", "call-cancel")]
    adapter.observe.assert_not_awaited()


@pytest.mark.asyncio
async def test_real_ledger_replay_does_not_count_a_second_skill_outcome(tmp_path: Path) -> None:
    adapter = MagicMock()
    adapter.observe = AsyncMock()
    skill_state = _runtime._State(adapter=adapter, active=True, workspace=tmp_path)
    skill_state.turn("", "run-replay").active_skill = "helper"
    _runtime.bind(skill_state)
    module_bus = ModuleBus()
    module_bus.subscribe("agent:post_tool", skills_post_tool, module_name="skills")
    bridge = create_arcrun_bridge(module_bus, bridge_only_tools=frozenset({"native"}))
    ledger = _Ledger()
    effects = 0

    async def execute(_args: dict[str, Any], _ctx: Any) -> str:
        nonlocal effects
        effects += 1
        return "done"

    native = Tool(
        name="native", description="native", input_schema={"type": "object"}, execute=execute
    )
    for _ in range(2):
        event_bus = EventBus(run_id="run-replay", on_event=bridge)
        state = RunState(
            messages=[],
            registry=RunToolRegistry(tools=[native], event_bus=event_bus),
            event_bus=event_bus,
            run_id="run-replay",
            tool_ledger=ledger,
        )
        _message, success = await execute_tool_call(
            _ToolCall("call-native", "native", {}),
            state,
            Sandbox(config=None, event_bus=event_bus),
        )
        assert success
        await _settle_bridge()

    assert effects == 1
    adapter.observe.assert_awaited_once()
