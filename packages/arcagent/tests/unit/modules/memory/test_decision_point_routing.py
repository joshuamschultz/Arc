"""RED — decision-point recall routes to the mid-loop buffer (SPEC-072 COMP-005).

For ``kind=="decision_point"`` (behind ``proactive_decision_point``, now honored) the
memory subscriber calls ``brain.on_moment`` and routes a non-empty result to the MID-LOOP
buffer (``arcagent.core.midloop_recall``) — NOT the assemble-prompt ``proactive_buffer`` —
so a decision-point recall reaches the model between loop steps. Disabled → no call, no
stage. Every other kind still uses the assemble buffer, unchanged (SPEC-071).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.core import midloop_recall
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import on_agent_moment
from arcagent.modules.memory.config import MemoryConfig

_DID = "did:arc:dp-routing-agent"
_BLOCK = '<memory-result source="decision-card">recall</memory-result>'


def _ctx(data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(data=data, agent_did=_DID)


class _Brain:
    def __init__(self, text: str) -> None:
        self._text = text
        self.calls: list[str] = []

    async def retrieve(self, query: str, **_: Any) -> str:
        return ""

    async def on_moment(self, kind: str, **_: Any) -> str:
        self.calls.append(kind)
        return self._text


def _install(brain: Any, cfg: dict[str, Any]) -> None:
    _runtime.bind(
        _runtime._State(
            config=MemoryConfig(**cfg),
            brain=brain,
            workspace=Path("."),
            telemetry=None,
            bus=None,
            agent_did=_DID,
            active=True,
        )
    )


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    midloop_recall.drain(_DID)
    yield
    _runtime.reset()
    midloop_recall.drain(_DID)


async def test_decision_point_routes_to_midloop_not_assemble_buffer() -> None:
    brain = _Brain(_BLOCK)
    _install(brain, {"proactive_decision_point": True})

    await on_agent_moment(
        _ctx({"kind": "decision_point", "cues": ["pick-db"], "text": "choose db", "session_id": None})
    )

    assert brain.calls == ["decision_point"]  # Brain consulted
    assert _runtime.state().proactive_buffer == []  # NOT the assemble buffer
    assert midloop_recall.drain(_DID) == [_BLOCK]  # routed mid-loop


async def test_decision_point_disabled_makes_no_call_and_stages_nothing() -> None:
    brain = _Brain(_BLOCK)
    _install(brain, {"proactive_decision_point": False})

    await on_agent_moment(
        _ctx({"kind": "decision_point", "cues": ["y"], "text": "y", "session_id": None})
    )

    assert brain.calls == []
    assert _runtime.state().proactive_buffer == []
    assert midloop_recall.drain(_DID) == []


async def test_pre_tool_moment_ignored_unless_pre_tool_opt_in() -> None:
    brain = _Brain(_BLOCK)
    _install(brain, {"proactive_decision_point": True})  # pre_tool NOT opted in

    await on_agent_moment(
        _ctx({"kind": "decision_point", "point": "pre_tool", "cues": ["deploy"],
              "text": "deploy()", "session_id": None})
    )

    assert brain.calls == []  # pre_tool site skipped
    assert midloop_recall.drain(_DID) == []


async def test_pre_tool_moment_staged_when_opted_in() -> None:
    brain = _Brain(_BLOCK)
    _install(brain, {"proactive_decision_point": True, "decision_point_pre_tool": True})

    await on_agent_moment(
        _ctx({"kind": "decision_point", "point": "pre_tool", "cues": ["deploy"],
              "text": "deploy()", "session_id": None})
    )

    assert brain.calls == ["decision_point"]
    assert midloop_recall.drain(_DID) == [_BLOCK]


async def test_pre_plan_moment_routes_regardless_of_pre_tool_flag() -> None:
    brain = _Brain(_BLOCK)
    _install(brain, {"proactive_decision_point": True})  # pre_tool off; pre_plan still routes

    await on_agent_moment(
        _ctx({"kind": "decision_point", "point": "pre_plan", "cues": [], "text": "",
              "session_id": None})
    )

    assert brain.calls == ["decision_point"]
    assert midloop_recall.drain(_DID) == [_BLOCK]


async def test_non_decision_point_kind_still_uses_assemble_buffer() -> None:
    brain = _Brain(_BLOCK)
    _install(brain, {})

    await on_agent_moment(
        _ctx({"kind": "entity_seen", "cues": ["ada"], "text": "ada", "session_id": None})
    )

    assert _runtime.state().proactive_buffer == [_BLOCK]
    assert midloop_recall.drain(_DID) == []
