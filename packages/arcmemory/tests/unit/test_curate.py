"""Input curation (#23) — drop mechanical tool plumbing, keep the conversation.

Pure/deterministic: reuses capture-time entity tags, no LLM/embedding. These
prove the keep/drop matrix, the config toggles, and citation integrity (order +
event_id preserved).
"""

from __future__ import annotations

from arcmemory.config import MemoryConfig
from arcmemory.curate import curate_for_distillation
from arcmemory.types import Event


def _ev(
    ident: str,
    kind: str,
    *,
    text: str | None = None,
    entities: list[str] | None = None,
    salience: float = 0.0,
) -> Event:
    return Event(
        event_id=ident,
        scope="did:a",
        kind=kind,
        text=text if text is not None else f"text-{ident}",
        salience=salience,
        entities=entities or [],
    )


def test_drops_bare_tool_plumbing_frame() -> None:
    # "tool:read -> ok" — short, no entity, read is not a knowledge tool.
    events = [_ev("t0", "tool", text="tool:read -> ok")]
    assert curate_for_distillation(events, MemoryConfig()) == []


def test_keeps_tool_that_touched_an_entity() -> None:
    events = [_ev("t0", "tool", text="tool:read -> ok", entities=["alice"])]
    assert curate_for_distillation(events, MemoryConfig()) == events


def test_keeps_knowledge_gathering_tool_even_when_short() -> None:
    # A web-search result is durable knowledge regardless of length.
    events = [_ev("t0", "tool", text="tool:web_search -> ok")]
    assert curate_for_distillation(events, MemoryConfig()) == events


def test_keeps_knowledge_creation_tool_even_when_short() -> None:
    events = [_ev("t0", "tool", text="tool:create_skill -> done")]
    assert curate_for_distillation(events, MemoryConfig()) == events


def test_keeps_substantive_length_tool_result() -> None:
    # A long bash result is real content though bash is not a knowledge tool.
    events = [_ev("t0", "tool", text="tool:bash -> " + "x" * 250)]
    assert curate_for_distillation(events, MemoryConfig()) == events


def test_custom_keep_tools_set_is_honored() -> None:
    cfg = MemoryConfig(curate_keep_tools=frozenset({"my_new_tool"}))
    kept = curate_for_distillation([_ev("t0", "tool", text="tool:my_new_tool -> x")], cfg)
    dropped = curate_for_distillation([_ev("t1", "tool", text="tool:web_search -> x")], cfg)
    assert [e.event_id for e in kept] == ["t0"]  # extended set keeps it
    assert dropped == []  # web_search no longer in the (overridden) set


def test_keeps_respond_user_and_observation_always() -> None:
    events = [
        _ev("r0", "respond"),
        _ev("u0", "user"),
        _ev("o0", "observation"),
        _ev("a0", "action"),
    ]
    assert curate_for_distillation(events, MemoryConfig()) == events


def test_toggle_off_is_identity() -> None:
    events = [_ev("t0", "tool"), _ev("r0", "respond")]
    assert curate_for_distillation(events, MemoryConfig(curate_input=False)) == events


def test_salience_escape_keeps_high_salience_tool() -> None:
    cfg = MemoryConfig(curate_tool_keep_salience=0.5)
    kept = curate_for_distillation([_ev("t0", "tool", salience=0.6)], cfg)
    dropped = curate_for_distillation([_ev("t1", "tool", salience=0.4)], cfg)
    assert [e.event_id for e in kept] == ["t0"]
    assert dropped == []


def test_default_zero_salience_does_not_keep_all_tools() -> None:
    # The escape hatch is OFF at 0.0 — otherwise every 0-salience tool would leak.
    assert curate_for_distillation([_ev("t0", "tool", salience=0.0)], MemoryConfig()) == []


def test_requires_entity_false_keeps_tools() -> None:
    cfg = MemoryConfig(curate_tool_requires_entity=False)
    events = [_ev("t0", "tool")]
    assert curate_for_distillation(events, cfg) == events


def test_preserves_order_and_event_ids() -> None:
    events = [
        _ev("o0", "observation"),
        _ev("t0", "tool"),  # dropped
        _ev("r0", "respond"),
        _ev("t1", "tool", entities=["bob"]),  # kept
        _ev("u0", "user"),
    ]
    result = curate_for_distillation(events, MemoryConfig())
    assert [e.event_id for e in result] == ["o0", "r0", "t1", "u0"]
