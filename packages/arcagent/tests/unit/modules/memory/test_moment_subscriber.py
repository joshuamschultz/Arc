"""Tests for the ``agent:moment`` proactive-buffer subscriber (SPEC-071).

* the ``@hook(event="agent:moment")`` subscriber in
  ``arcagent.modules.memory.capabilities`` — expected name/signature::

      @hook(event="agent:moment")
      async def on_agent_moment(ctx: Any) -> None

  It guards ``st.active`` and ``st.config.proactive_enabled``, calls the Brain's
  optional ``on_moment(kind, *, cues=..., text=..., clearance="unclassified",
  session_id=..., ...)`` via the ``getattr(st.brain, "on_moment", None)``
  optional-method pattern (mirrors ``_acl_allows``'s ``authorize`` lookup), and
  appends any non-empty returned text to a NEW ``_State.proactive_buffer:
  list[str]`` field (``packages/arcagent/src/arcagent/modules/memory/_runtime.py``).

* ``ContextRetrieval`` draining that buffer into candidates, once.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.brain import NullBrain
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import ContextRetrieval, on_agent_moment

_DID = "did:arc:test-agent"


def _ctx(data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(data=data, agent_did=_DID)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


class _MomentSpyBrain:
    """Spy Brain recording ``on_moment`` calls; both ``retrieve``/``on_moment`` canned.

    Mirrors ``test_memory_wiring.py``'s ``_SpyBrain`` shape but adds the new
    optional ``on_moment`` method under test.
    """

    def __init__(self, *, moment_text: str = "<memory-result>proactive</memory-result>") -> None:
        self.retrieves: list[str] = []
        self.moment_calls: list[dict[str, Any]] = []
        self._moment_text = moment_text

    async def retrieve(self, query: str, **_: Any) -> str:
        self.retrieves.append(query)
        return f"<memory-result>{query}</memory-result>"

    async def on_moment(self, kind: str, **kw: Any) -> str:
        self.moment_calls.append({"kind": kind, **kw})
        return self._moment_text


def _configure_with(brain: Any, cfg: dict[str, Any] | None = None) -> None:
    """Install a spy/real brain directly into runtime state (bypass select).

    Same pattern as ``test_memory_wiring.py::_configure_with``.
    """
    from arcagent.modules.memory.config import MemoryConfig

    _runtime.bind(
        _runtime._State(
            config=MemoryConfig(**(cfg or {})),
            brain=brain,
            workspace=Path("."),
            telemetry=None,
            bus=None,
            agent_did=_DID,
            active=not isinstance(brain, NullBrain),
        )
    )


# -- 1. Buffers on fire ----------------------------------------------------


async def test_on_moment_subscriber_calls_brain_and_buffers_returned_text() -> None:
    spy = _MomentSpyBrain()
    _configure_with(spy)

    await on_agent_moment(
        _ctx(
            {
                "kind": "entity_seen",
                "cues": ["ada"],
                "text": "ada owns payments",
                "session_id": None,
            }
        )
    )

    assert len(spy.moment_calls) == 1
    call = spy.moment_calls[0]
    assert call["kind"] == "entity_seen"
    assert call["cues"] == ["ada"]
    assert call["text"] == "ada owns payments"
    assert call["session_id"] is None
    assert call.get("clearance") == "unclassified"

    assert _runtime.state().proactive_buffer == ["<memory-result>proactive</memory-result>"]


# -- 2. The next context retrieval drains the buffer ------------------------


async def test_context_retrieval_drains_the_staged_buffer_once() -> None:
    spy = _MomentSpyBrain()
    _configure_with(spy)

    await on_agent_moment(
        _ctx(
            {
                "kind": "topic_shift",
                "cues": ["payments"],
                "text": "payments",
                "session_id": None,
            }
        )
    )
    assert _runtime.state().proactive_buffer  # sanity: staged before retrieval runs

    first = await ContextRetrieval().retrieve("who owns payments", memory_top_k=4, docs_top_k=3)
    second = await ContextRetrieval().retrieve("who owns payments", memory_top_k=4, docs_top_k=3)

    assert "<memory-result>proactive</memory-result>" in [c["text"] for c in first["candidates"]]
    assert "<memory-result>proactive</memory-result>" not in [
        c["text"] for c in second["candidates"]
    ]
    assert _runtime.state().proactive_buffer == []


# -- 3. proactive_enabled=False -> no-op ------------------------------------


async def test_on_moment_subscriber_noop_when_proactive_disabled() -> None:
    spy = _MomentSpyBrain()
    _configure_with(spy, {"proactive_enabled": False})

    await on_agent_moment(
        _ctx({"kind": "decision_point", "cues": ["x"], "text": "x", "session_id": None})
    )

    assert spy.moment_calls == []  # brain never consulted
    assert _runtime.state().proactive_buffer == []


# -- 4. Empty on_moment return stages nothing ------------------------------


async def test_empty_moment_text_buffers_nothing_and_retrieves_nothing_staged() -> None:
    spy = _MomentSpyBrain(moment_text="")
    _configure_with(spy)

    await on_agent_moment(
        _ctx(
            {
                "kind": "task_start",
                "cues": ["x"],
                "text": "run the deploy",
                "session_id": None,
            }
        )
    )
    assert len(spy.moment_calls) == 1  # brain WAS consulted
    assert _runtime.state().proactive_buffer == []  # but empty text never buffers

    found = await ContextRetrieval().retrieve("unrelated query", memory_top_k=4, docs_top_k=3)
    assert found["candidates"] == []


# -- 5. NullBrain / inactive -> no-op, never raises --------------------------


async def test_on_moment_subscriber_noop_with_null_brain() -> None:
    _configure_with(NullBrain())

    await on_agent_moment(
        _ctx({"kind": "entity_seen", "cues": ["ada"], "text": "ada", "session_id": None})
    )  # must not raise

    assert _runtime.state().proactive_buffer == []
