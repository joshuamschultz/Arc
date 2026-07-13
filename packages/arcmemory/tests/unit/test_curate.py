"""Input curation — feed distillation only the session conversation.

Pure/deterministic kind-based filter (no LLM/embedding): the user's turns and the
agent's responses are kept; ``tool`` frames and every other operational kind are
dropped before distillation, so the model never sees — and cannot distill — the
agent's own machinery. These prove the keep/drop matrix, the config toggle, the
custom kind set, and citation integrity (order + event_id preserved).
"""

from __future__ import annotations

from arcmemory.config import MemoryConfig
from arcmemory.curate import curate_for_distillation
from arcmemory.types import Event


def _ev(ident: str, kind: str, *, text: str | None = None) -> Event:
    return Event(
        event_id=ident,
        scope="did:a",
        kind=kind,
        text=text if text is not None else f"text-{ident}",
    )


def test_keeps_user_and_respond() -> None:
    events = [_ev("u0", "user"), _ev("r0", "respond")]
    assert curate_for_distillation(events, MemoryConfig()) == events


def test_drops_tool_frames() -> None:
    # Every tool frame is dropped — even a long, substantive-looking one.
    events = [
        _ev("t0", "tool", text="tool:read -> ok"),
        _ev("t1", "tool", text="tool:web_search -> " + "x" * 400),
    ]
    assert curate_for_distillation(events, MemoryConfig()) == []


def test_drops_non_conversation_kinds() -> None:
    # Only the configured conversation kinds survive; anything else is operational.
    events = [_ev("o0", "observation"), _ev("a0", "action"), _ev("m0", "message")]
    assert curate_for_distillation(events, MemoryConfig()) == []


def test_custom_conversation_kinds_is_honored() -> None:
    cfg = MemoryConfig(curate_conversation_kinds=frozenset({"user"}))
    result = curate_for_distillation([_ev("u0", "user"), _ev("r0", "respond")], cfg)
    assert [e.event_id for e in result] == ["u0"]  # respond no longer in the set


def test_toggle_off_is_identity() -> None:
    events = [_ev("t0", "tool"), _ev("r0", "respond")]
    assert curate_for_distillation(events, MemoryConfig(curate_input=False)) == events


def test_preserves_order_and_event_ids() -> None:
    events = [
        _ev("u0", "user"),
        _ev("t0", "tool"),  # dropped
        _ev("r0", "respond"),
        _ev("t1", "tool"),  # dropped
        _ev("u1", "user"),
    ]
    result = curate_for_distillation(events, MemoryConfig())
    assert [e.event_id for e in result] == ["u0", "r0", "u1"]
