"""SPEC-083 T-1230 (REQ-513, REQ-514; COMP-030, COMP-031) — the selection prompt is editable.

Strategy selection used to send a hard-coded system message ("Select the best
execution strategy for the task below..."). An operator could edit every other
prompt an agent sends but not this one, so the call that decides *how* a turn
runs was the one call an operator could not steer.

The contract under test:

* ``arcrun.run(..., prompt_source=...)`` accepts an ``arcprompt.PromptSource``
  (``resolve(package, name) -> str``). With no source, arcrun uses
  ``arcprompt.StockPromptSource`` — the shipped ``arcrun/context/*.md`` bodies.
* The selection call's system message is ``arcrun/strategy_select`` plus each
  allowed strategy's ``strategy_<name>_description``, all resolved through that
  source. No inline selection text remains.
* After selection, the turns of the chosen strategy carry its ``strategy_<name>``
  guidance, resolved through the same source.
* A failed or invalid selection falls back to ``react`` and records the fallback
  on the event bus; selection tokens count toward the run's usage.

Everything is driven through the public ``arcrun.run`` entry point with a fake
model at the ``model.invoke`` wire — the only thing faked is the LLM.
"""

from __future__ import annotations

from typing import Any

import pytest
from arcprompt import load_stock
from packages.arcrun.tests.conftest import LLMResponse, ToolCall, Usage

from arcrun import StaticProvider, run
from arcrun._messages import content_text
from arcrun.types import Tool

_TASK = "Please summarise the Q3 report and total the line items."
_OLD_INLINE_SELECTION_TEXT = "Select the best execution strategy for the task below"


# --------------------------------------------------------------------------- fakes


class _MarkerSource:
    """A PromptSource that answers every lookup with a traceable marker.

    Each body is ``MARKER::<package>/<name>`` so a test can prove exactly which
    prompt reached the wire, and every lookup is recorded.
    """

    def __init__(self) -> None:
        self.lookups: list[tuple[str, str]] = []

    def resolve(self, package: str, name: str) -> str:
        self.lookups.append((package, name))
        return _marker(package, name)


def _marker(package: str, name: str) -> str:
    return f"MARKER::{package}/{name}"


class _WireModel:
    """Fake LLM at the ``model.invoke`` boundary: scripted replies, full call log."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def invoke(
        self, messages: list[Any], tools: list[Any] | None = None, **kwargs: Any
    ) -> Any:
        self.calls.append({"messages": list(messages), "tools": tools})
        if not self._responses:
            raise AssertionError("fake model exhausted — an unexpected extra model call")
        reply = self._responses.pop(0)
        if isinstance(reply, BaseException):
            raise reply
        return reply

    def selection_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if _is_selection(c["tools"])]

    def task_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if not _is_selection(c["tools"])]


def _is_selection(tools: list[Any] | None) -> bool:
    return any(getattr(t, "name", "") == "select_strategy" for t in tools or [])


def _select(strategy: str, usage: Usage | None = None) -> LLMResponse:
    return LLMResponse(
        tool_calls=[ToolCall(id="sel", name="select_strategy", arguments={"strategy": strategy})],
        stop_reason="tool_use",
        usage=usage or Usage(input_tokens=0, output_tokens=0, total_tokens=0),
        cost_usd=0.0,
    )


def _final(text: str = "done", usage: Usage | None = None) -> LLMResponse:
    return LLMResponse(content=text, stop_reason="end_turn", usage=usage or Usage())


async def _echo(params: dict[str, Any], ctx: object) -> str:
    return "echo"


def _provider() -> StaticProvider:
    return StaticProvider(
        [
            Tool(
                name="echo",
                description="Echo input",
                input_schema={"type": "object", "properties": {"input": {"type": "string"}}},
                execute=_echo,
            )
        ]
    )


def _tagged(name: str) -> str:
    """The chosen strategy's guidance as it reaches the wire: a ``<strategy_NAME>`` block."""
    return f"<strategy_{name}>\n{_marker('arcrun', f'strategy_{name}')}\n</strategy_{name}>"


def _system_text(call: dict[str, Any]) -> str:
    return "\n".join(
        content_text(m.content) for m in call["messages"] if getattr(m, "role", "") == "system"
    )


def _user_text(call: dict[str, Any]) -> str:
    return "\n".join(
        content_text(m.content) for m in call["messages"] if getattr(m, "role", "") == "user"
    )


def _select_enum(call: dict[str, Any]) -> list[str]:
    tool = next(t for t in call["tools"] if getattr(t, "name", "") == "select_strategy")
    return list(tool.parameters["properties"]["strategy"]["enum"])


async def _run(model: _WireModel, allowed: list[str] | None, **kwargs: Any) -> Any:
    return await run(model, _provider(), "Be helpful.", _TASK, allowed_strategies=allowed, **kwargs)


# ------------------------------------------------ (b) selection prompt via the source


@pytest.mark.asyncio
async def test_select_strategy_system_message_is_strategy_select_resolved_through_source() -> None:
    source = _MarkerSource()
    model = _WireModel([_select("react"), _final()])

    await _run(model, ["react", "code"], prompt_source=source)

    [selection] = model.selection_calls()
    assert _marker("arcrun", "strategy_select") in _system_text(selection)
    assert ("arcrun", "strategy_select") in source.lookups


@pytest.mark.asyncio
async def test_select_strategy_descriptions_resolved_through_source_for_each_allowed() -> None:
    source = _MarkerSource()
    model = _WireModel([_select("react"), _final()])

    await _run(model, ["react", "code", "dynamic"], prompt_source=source)

    system = _system_text(model.selection_calls()[0])
    for name in ("react", "code", "dynamic"):
        assert _marker("arcrun", f"strategy_{name}_description") in system
    # A strategy that is not on the table is not described to the selector.
    assert _marker("arcrun", "strategy_plan_execute_description") not in system
    assert _marker("arcrun", "strategy_oneshot_description") not in system


@pytest.mark.asyncio
async def test_select_strategy_sends_no_inline_selection_text_when_source_supplied() -> None:
    model = _WireModel([_select("react"), _final()])

    await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    assert _OLD_INLINE_SELECTION_TEXT not in _system_text(model.selection_calls()[0])


@pytest.mark.asyncio
async def test_select_strategy_default_source_sends_stock_strategy_select_file() -> None:
    # No prompt_source => StockPromptSource => the shipped arcrun/context file.
    stock = load_stock("arcrun", "strategy_select").strip()
    assert stock, "arcrun/context/strategy_select.md must carry real selection guidance"
    model = _WireModel([_select("react"), _final()])

    await _run(model, ["react", "code"])

    system = _system_text(model.selection_calls()[0])
    assert stock in system
    assert _OLD_INLINE_SELECTION_TEXT not in system


@pytest.mark.asyncio
async def test_select_strategy_explicit_stock_source_matches_shipped_file() -> None:
    from arcprompt import StockPromptSource

    source = StockPromptSource()
    assert (
        source.resolve("arcrun", "strategy_select").strip()
        == load_stock("arcrun", "strategy_select").strip()
    )
    model = _WireModel([_select("react"), _final()])

    await _run(model, ["react", "code"], prompt_source=source)

    system = _system_text(model.selection_calls()[0])
    assert load_stock("arcrun", "strategy_select").strip() in system
    assert load_stock("arcrun", "strategy_code_description").strip() in system


@pytest.mark.asyncio
async def test_select_strategy_task_text_stays_in_user_message_not_system() -> None:
    # LLM01: the task is untrusted content; it must never be spliced into the
    # instruction channel of the selection call.
    model = _WireModel([_select("react"), _final()])

    await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    selection = model.selection_calls()[0]
    assert _TASK not in _system_text(selection)
    assert _TASK in _user_text(selection)


@pytest.mark.asyncio
async def test_select_strategy_tool_enum_is_exactly_the_allowed_set() -> None:
    model = _WireModel([_select("react"), _final()])

    await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    assert sorted(_select_enum(model.selection_calls()[0])) == ["code", "react"]


# ------------------------------------------- (c) chosen strategy's guidance is sent


@pytest.mark.asyncio
async def test_chosen_code_strategy_turns_carry_code_guidance_from_source() -> None:
    model = _WireModel([_select("code"), _final()])

    result = await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    assert result.strategy_used == "code"
    [task_call] = model.task_calls()
    # Tagged, so a trace viewer can attribute the block to the chosen strategy.
    assert _tagged("code") in _system_text(task_call)


@pytest.mark.asyncio
async def test_chosen_react_strategy_turns_carry_react_not_code_guidance() -> None:
    model = _WireModel([_select("react"), _final()])

    result = await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    assert result.strategy_used == "react"
    [task_call] = model.task_calls()
    system = _system_text(task_call)
    assert _tagged("react") in system
    # Narrowness: the strategy that was NOT chosen does not steer this run.
    assert _marker("arcrun", "strategy_code") not in system


# ------------------------------------------------- (d) fallback is react + recorded


def _fallbacks(result: Any) -> list[Any]:
    return [e for e in result.events if e.type == "strategy.selection.fallback"]


@pytest.mark.asyncio
async def test_selection_invalid_choice_falls_back_to_react_and_records_it() -> None:
    model = _WireModel([_select("nonexistent"), _final()])

    result = await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    assert result.strategy_used == "react"
    [fallback] = _fallbacks(result)
    assert fallback.data["defaulted_to"] == "react"


@pytest.mark.asyncio
async def test_selection_installed_but_not_allowed_choice_falls_back_to_react() -> None:
    # plan_execute is installed but not on the table: the model cannot widen it.
    model = _WireModel([_select("plan_execute"), _final()])

    result = await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    assert result.strategy_used == "react"
    assert len(_fallbacks(result)) == 1


@pytest.mark.asyncio
async def test_selection_with_no_tool_call_falls_back_to_react_and_records_it() -> None:
    model = _WireModel([LLMResponse(content="I pick code", stop_reason="end_turn"), _final()])

    result = await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    assert result.strategy_used == "react"
    assert len(_fallbacks(result)) == 1


@pytest.mark.asyncio
async def test_selection_model_error_falls_back_to_react_and_records_it() -> None:
    model = _WireModel([ConnectionError("provider down"), _final()])

    result = await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    assert result.strategy_used == "react"
    assert len(_fallbacks(result)) == 1
    assert any(e.type == "strategy.selection.error" for e in result.events)


# ------------------------------------------------ (e) selection tokens are charged


@pytest.mark.asyncio
async def test_selection_tokens_accumulate_into_run_usage() -> None:
    model = _WireModel(
        [
            _select("react", usage=Usage(input_tokens=40, output_tokens=8, total_tokens=48)),
            _final(usage=Usage(input_tokens=10, output_tokens=5, total_tokens=15)),
        ]
    )

    result = await _run(model, ["react", "code"], prompt_source=_MarkerSource())

    assert result.tokens_used["total"] == 63
    assert result.tokens_used["input"] == 50
    assert result.tokens_used["output"] == 13
