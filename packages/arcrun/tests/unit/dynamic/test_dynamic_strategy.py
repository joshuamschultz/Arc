"""The strategy is the seam where a model-authored plan becomes a real run.

Two properties matter more than the happy path. A script the model got wrong
must degrade to the ordinary ReAct loop rather than failing the run — the user
asked a question, not for a scripting exercise. And a script that paused or
failed must be reported as paused or failed, because a half-finished run that
looks finished is the worst outcome available.
"""

from __future__ import annotations

from typing import Any

import pytest
from packages.arcrun.tests.conftest import LLMResponse, ToolCall

from arcrun._messages import content_text, system_message, user_message
from arcrun.dynamic.host import ScriptOutcome
from arcrun.events import EventBus
from arcrun.registry import ToolRegistry
from arcrun.sandbox import Sandbox
from arcrun.state import RunState
from arcrun.strategies import Strategy
from arcrun.strategies.dynamic import DynamicStrategy, _result_from_outcome
from arcrun.types import Tool

GOOD_SCRIPT = 'phase("work")\nr = agent("look into it")\ncomplete({"answer": r["output"]})\n'
PAUSING_SCRIPT = 'phase("work")\npause("verification", "a human must confirm")\n'
BAD_SCRIPT = "import os\n"
# The authoring prompt tells the model the run input arrives as ``args``, so a
# script that reads it is the ordinary case, not an exotic one.
ARGS_SCRIPT = 'complete({"echo": args["task"]})\n'


async def _noop(_params: dict[str, Any], _ctx: object) -> str:
    return "ok"


def _registry(bus: EventBus) -> ToolRegistry:
    registry = ToolRegistry(
        tools=[
            Tool(
                name="read_file",
                description="read a file",
                input_schema={"type": "object", "properties": {}},
                execute=_noop,
                classification="read_only",
            )
        ],
        event_bus=bus,
    )
    registry.freeze()
    return registry


def _state(bus: EventBus) -> RunState:
    return RunState(
        messages=[system_message("You are the parent."), user_message("Research the thing.")],
        registry=_registry(bus),
        event_bus=bus,
        run_id="parent-run",
        strategy_name="dynamic",
    )


class AuthoringModel:
    """Emits a queued script when asked to author, and answers child runs itself.

    The two roles are told apart by the tool set, which is how the real
    providers see them: only the authoring call is offered ``emit_script``.
    """

    def __init__(self, scripts: list[str], child_reply: str = "child findings") -> None:
        self._scripts = list(scripts)
        self._child_reply = child_reply
        self.authoring_prompts: list[str] = []
        self.child_prompts: list[str] = []
        self.raise_on_author = False

    async def invoke(
        self, messages: list[Any], tools: list[Any] | None = None, **_kwargs: Any
    ) -> LLMResponse:
        if any(t.name == "emit_script" for t in (tools or [])):
            return self._author(messages)
        self.child_prompts.append(content_text(messages[-1].content))
        return LLMResponse(content=self._child_reply, stop_reason="end_turn")

    def _author(self, messages: list[Any]) -> LLMResponse:
        self.authoring_prompts.append(content_text(messages[-1].content))
        if self.raise_on_author:
            raise RuntimeError("provider is down")
        source = self._scripts.pop(0) if self._scripts else ""
        return LLMResponse(
            content=None,
            tool_calls=[ToolCall(id="author-1", name="emit_script", arguments={"source": source})],
            stop_reason="tool_use",
        )


async def _run(model: AuthoringModel, state: RunState, bus: EventBus) -> Any:
    return await DynamicStrategy()(model, state, Sandbox(config=None, event_bus=bus), max_turns=6)


def _types(bus: EventBus) -> list[str]:
    return [event.type for event in bus.events]


# --- shape -------------------------------------------------------------------


def test_dynamic_is_a_strategy_with_stock_text() -> None:
    strategy = DynamicStrategy()

    assert isinstance(strategy, Strategy)
    assert strategy.name == "dynamic"
    assert strategy.description.strip()
    assert strategy.prompt_guidance.strip()


# --- the working path --------------------------------------------------------


@pytest.mark.asyncio
async def test_a_valid_script_runs_and_its_result_becomes_the_completion() -> None:
    bus = EventBus(run_id="parent-run")
    model = AuthoringModel([GOOD_SCRIPT])
    state = _state(bus)

    result = await _run(model, state, bus)

    assert result.completion_payload is not None
    assert result.completion_payload["status"] == "success"
    assert result.completion_payload["result"] == {"answer": "child findings"}
    assert result.content == result.completion_payload["summary"]
    assert result.strategy_used == "dynamic"


@pytest.mark.asyncio
async def test_the_script_really_spawns_a_child_run() -> None:
    """``agent()`` is not a simulation — it is a bounded run against the model."""
    bus = EventBus(run_id="parent-run")
    model = AuthoringModel([GOOD_SCRIPT])

    await _run(model, _state(bus), bus)

    assert any("look into it" in prompt for prompt in model.child_prompts)


@pytest.mark.asyncio
async def test_the_working_path_narrates_itself_on_the_event_chain() -> None:
    bus = EventBus(run_id="parent-run")

    await _run(AuthoringModel([GOOD_SCRIPT]), _state(bus), bus)

    types = _types(bus)
    assert "dynamic.authored" in types
    assert "dynamic.validated" in types
    assert "dynamic.completed" in types
    assert "dynamic.fallback" not in types


@pytest.mark.asyncio
async def test_the_run_input_reaches_the_script_as_args() -> None:
    """A script reading ``args`` must survive both the dry run and the live run."""
    bus = EventBus(run_id="parent-run")

    result = await _run(AuthoringModel([ARGS_SCRIPT]), _state(bus), bus)

    assert result.completion_payload is not None
    assert result.completion_payload["result"] == {"echo": "Research the thing."}


@pytest.mark.asyncio
async def test_the_task_reaches_the_author_as_the_thing_to_plan_for() -> None:
    bus = EventBus(run_id="parent-run")
    model = AuthoringModel([GOOD_SCRIPT])

    await _run(model, _state(bus), bus)

    assert "Research the thing." in model.authoring_prompts[0]


# --- correction and fallback -------------------------------------------------


@pytest.mark.asyncio
async def test_a_rejected_script_is_reauthored_once_with_the_error_fed_back() -> None:
    bus = EventBus(run_id="parent-run")
    model = AuthoringModel([BAD_SCRIPT, GOOD_SCRIPT])

    result = await _run(model, _state(bus), bus)

    assert len(model.authoring_prompts) == 2
    assert "import" in model.authoring_prompts[1]
    assert result.completion_payload is not None
    assert result.completion_payload["status"] == "success"
    assert "dynamic.rejected" in _types(bus)


@pytest.mark.asyncio
async def test_two_bad_scripts_degrade_to_the_ordinary_loop() -> None:
    """A bad plan costs a retry, never the user's answer."""
    bus = EventBus(run_id="parent-run")
    model = AuthoringModel([BAD_SCRIPT, BAD_SCRIPT])

    result = await _run(model, _state(bus), bus)

    assert len(model.authoring_prompts) == 2
    assert result.content == "child findings"
    assert "dynamic.fallback" in _types(bus)
    assert "dynamic.completed" not in _types(bus)


@pytest.mark.asyncio
async def test_an_author_that_emits_nothing_falls_back_rather_than_running_empty() -> None:
    bus = EventBus(run_id="parent-run")
    model = AuthoringModel(["", ""])

    result = await _run(model, _state(bus), bus)

    assert result.content == "child findings"
    assert "dynamic.fallback" in _types(bus)


@pytest.mark.asyncio
async def test_a_provider_failure_during_authoring_falls_back_too() -> None:
    bus = EventBus(run_id="parent-run")
    model = AuthoringModel([GOOD_SCRIPT])
    model.raise_on_author = True

    result = await _run(model, _state(bus), bus)

    assert result.content == "child findings"
    assert "dynamic.author.error" in _types(bus)
    assert "dynamic.fallback" in _types(bus)


# --- honest endings ----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_paused_script_is_reported_as_partial_not_success() -> None:
    bus = EventBus(run_id="parent-run")

    result = await _run(AuthoringModel([PAUSING_SCRIPT]), _state(bus), bus)

    assert result.completion_payload is not None
    assert result.completion_payload["status"] == "partial"
    assert "a human must confirm" in result.completion_payload["summary"]


def test_a_failed_script_is_reported_as_failed() -> None:
    bus = EventBus(run_id="parent-run")
    state = _state(bus)

    result = _result_from_outcome(
        state, ScriptOutcome(status="failed", error="the host refused", phases_seen=["work"])
    )

    assert result.completion_payload is not None
    assert result.completion_payload["status"] == "failed"
    assert result.completion_payload["error"] == "the host refused"
    assert "the host refused" in result.content


def test_a_budget_exceeded_script_is_reported_as_failed() -> None:
    bus = EventBus(run_id="parent-run")
    state = _state(bus)

    result = _result_from_outcome(
        state, ScriptOutcome(status="budget_exceeded", message="out of agent calls")
    )

    assert result.completion_payload is not None
    assert result.completion_payload["status"] == "failed"
    assert result.completion_payload["error"] == "budget_exceeded"
