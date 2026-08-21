"""Failing tests for the ``agent:moment`` proactive-buffer subscriber (SPEC-071 T-988).

Covers the two not-yet-built pieces (T-989 builds them):

* a NEW ``@hook(event="agent:moment")`` subscriber in
  ``arcagent.modules.memory.capabilities`` — expected name/signature::

      @hook(event="agent:moment")
      async def on_agent_moment(ctx: Any) -> None

  It guards ``st.active`` and ``st.config.proactive_enabled``, calls the Brain's
  optional ``on_moment(kind, *, cues=..., text=..., clearance="unclassified",
  session_id=..., ...)`` via the ``getattr(st.brain, "on_moment", None)``
  optional-method pattern (mirrors ``_acl_allows``'s ``authorize`` lookup), and
  appends any non-empty returned text to a NEW ``_State.proactive_buffer:
  list[str]`` field (``packages/arcagent/src/arcagent/modules/memory/_runtime.py``).

* the EXISTING ``inject_recall`` (``agent:assemble_prompt``) draining that buffer
  and merging it into ``sections["recall"]`` — appended after, deduped against
  the query-driven recall text, buffer cleared afterward.

RED discipline: importing ``on_agent_moment`` from ``capabilities`` fails today
(the name does not exist) — that IS the correct RED signal for a not-yet-built
handler (see task brief). Every test body still asserts real behavior beyond
the import, so GREEN (T-989) is driven by these assertions, not merely by the
import succeeding.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.brain import NullBrain
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import inject_recall, on_agent_moment

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

    def __init__(
        self, *, moment_text: str = "<memory-result>proactive</memory-result>"
    ) -> None:
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


# -- 2. Injection merge -----------------------------------------------------


async def test_inject_recall_drains_and_merges_proactive_buffer() -> None:
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
    assert _runtime.state().proactive_buffer  # sanity: buffered before assemble runs

    sections: dict[str, str] = {}
    await inject_recall(_ctx({"sections": sections, "query": "who owns payments"}))

    assert "recall" in sections
    assert "<memory-result>proactive</memory-result>" in sections["recall"]
    assert "<memory-result>who owns payments</memory-result>" in sections["recall"]
    # Drained AND cleared — a second assemble in the same turn must not re-inject it.
    assert _runtime.state().proactive_buffer == []


async def test_inject_recall_dedupes_identical_proactive_and_query_text() -> None:
    """Query-driven recall and a proactive buffer surfacing the SAME card must not
    duplicate it in the assembled section — merge is a dedup, not a blind append."""
    shared_text = "<memory-result>same card</memory-result>"
    spy = _MomentSpyBrain(moment_text=shared_text)
    _configure_with(spy)

    async def _same_retrieve(query: str, **_: Any) -> str:
        return shared_text

    spy.retrieve = _same_retrieve  # type: ignore[method-assign]

    await on_agent_moment(
        _ctx({"kind": "task_start", "cues": ["x"], "text": "x", "session_id": None})
    )
    sections: dict[str, str] = {}
    await inject_recall(_ctx({"sections": sections, "query": "same card"}))

    assert sections["recall"].count(shared_text) == 1


# -- 3. proactive_enabled=False -> no-op ------------------------------------


async def test_on_moment_subscriber_noop_when_proactive_disabled() -> None:
    spy = _MomentSpyBrain()
    _configure_with(spy, {"proactive_enabled": False})

    await on_agent_moment(
        _ctx({"kind": "decision_point", "cues": ["x"], "text": "x", "session_id": None})
    )

    assert spy.moment_calls == []  # brain never consulted
    assert _runtime.state().proactive_buffer == []


# -- 4. Empty on_moment return injects nothing -------------------------------


async def test_empty_moment_text_buffers_nothing_and_injects_nothing() -> None:
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

    sections: dict[str, str] = {}
    await inject_recall(_ctx({"sections": sections, "query": "unrelated query"}))
    # Query-driven recall still runs (unaffected), but nothing proactive rode along.
    assert "<memory-result>proactive</memory-result>" not in sections.get("recall", "")


# -- 5. NullBrain / inactive -> no-op, never raises --------------------------


async def test_on_moment_subscriber_noop_with_null_brain() -> None:
    _configure_with(NullBrain())

    await on_agent_moment(
        _ctx({"kind": "entity_seen", "cues": ["ada"], "text": "ada", "session_id": None})
    )  # must not raise

    assert _runtime.state().proactive_buffer == []
