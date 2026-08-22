from __future__ import annotations

from collections.abc import AsyncIterator

import arcrun

from arccli.commands.agent.run import _collect_agent_stream


async def _events() -> AsyncIterator[arcrun.StreamEvent]:
    yield arcrun.TokenEvent(text="hel")
    yield arcrun.TokenEvent(text="lo")
    yield arcrun.TurnEndEvent(final_text="hello", turns=1, cost_usd=0.01)


async def _terminal_only() -> AsyncIterator[arcrun.StreamEvent]:
    yield arcrun.TurnEndEvent(final_text="hello", turns=1)


async def test_collect_agent_stream_renders_tokens_and_uses_terminal_totals(capsys) -> None:
    result, streamed = await _collect_agent_stream(_events(), emit_tokens=True)

    assert streamed
    assert capsys.readouterr().out == "hello"
    assert result.content == "hello"
    assert result.turns == 1
    assert result.cost_usd == 0.01


async def test_collect_agent_stream_can_suppress_rendering_for_json(capsys) -> None:
    result, streamed = await _collect_agent_stream(_events(), emit_tokens=False)

    assert not streamed
    assert capsys.readouterr().out == ""
    assert result.content == "hello"


async def test_collect_agent_stream_falls_back_to_terminal_when_no_tokens(capsys) -> None:
    result, streamed = await _collect_agent_stream(_terminal_only(), emit_tokens=True)

    assert not streamed
    assert capsys.readouterr().out == ""
    assert result.content == "hello"
