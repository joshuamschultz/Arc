"""The single arcrun adapter — breach/timeout/absence all map to a degrade signal."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from arcmemory import react_adapter
from arcmemory.react_adapter import ReactOutcome, _outcome_from_result, run_react_loop


@dataclass
class _FakeResult:
    content: str | None
    turns: int = 1
    tool_calls_made: int = 0
    tokens_used: dict[str, Any] | None = None
    completion_payload: dict[str, Any] | None = None


def test_clean_completion_is_not_degraded() -> None:
    out = _outcome_from_result(_FakeResult("done", completion_payload={"status": "success"}))
    assert out.degraded is False
    assert out.content == "done"


def test_end_turn_no_payload_is_not_degraded() -> None:
    out = _outcome_from_result(_FakeResult("stopped", completion_payload=None))
    assert out.degraded is False


def test_max_turns_breach_maps_to_degrade() -> None:
    payload = {"status": "failed", "error": "max_turns", "summary": "hit turn cap"}
    out = _outcome_from_result(_FakeResult(None, completion_payload=payload))
    assert out.degraded is True
    assert out.reason == "max_turns"


def test_runaway_loop_breach_maps_to_degrade() -> None:
    payload = {"status": "failed", "error": "runaway_loop", "summary": "stuck"}
    out = _outcome_from_result(_FakeResult(None, completion_payload=payload))
    assert out.degraded is True
    assert out.reason == "runaway_loop"


async def test_arcrun_absent_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(react_adapter, "_ARCRUN_AVAILABLE", False)
    out = await run_react_loop(
        model=object(),
        tools=[],
        system_prompt="s",
        task="t",
        max_turns=4,
        max_tokens=1000,
        timeout_seconds=5.0,
        actor_did="did:arc:default:memory/abc",
    )
    assert isinstance(out, ReactOutcome)
    assert out.degraded is True
    assert out.reason == "arcrun-absent"


async def test_timeout_degrades(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _hang(*_a: Any, **_k: Any) -> Any:
        import asyncio

        await asyncio.sleep(10)

    monkeypatch.setattr(react_adapter, "_ARCRUN_AVAILABLE", True)
    monkeypatch.setattr(react_adapter, "run", _hang)
    monkeypatch.setattr(react_adapter, "StaticProvider", lambda tools: object())
    out = await run_react_loop(
        model=object(),
        tools=[],
        system_prompt="s",
        task="t",
        max_turns=4,
        max_tokens=1000,
        timeout_seconds=0.05,
        actor_did="did:arc:default:memory/abc",
    )
    assert out.degraded is True
    assert out.reason == "timeout"
