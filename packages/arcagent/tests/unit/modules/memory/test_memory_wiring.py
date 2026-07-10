"""Thin memory wiring + the Brain-seam acceptance tests (SPEC-041 Phase 8).

Two acceptance proofs live here (DECISIONS-LOCKED hard requirement):

* **memory-less** — with ``brain="none"`` the agent runs, every hook is a silent
  no-op, and NOT ONE file is written under ``workspace/memory``;
* **wired** — with ``brain="arcmemory"`` capture writes glass-box files and recall
  activates.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.brain import NullBrain
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import (
    capture_respond,
    capture_tool,
    consolidate_poll_once,
    inject_memory_disabled_note,
    inject_recall,
    memory_search,
)

_DID = "did:arc:test-agent"


def _ctx(data: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(data=data, agent_did=_DID)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


class _SpyBrain:
    """Records Brain calls; retrieve returns a canned recall."""

    def __init__(self) -> None:
        self.captures: list[str] = []
        self.retrieves: list[str] = []
        self.consolidations = 0

    async def capture(self, text: str, **_: Any) -> None:
        self.captures.append(text)

    async def retrieve(self, query: str, **_: Any) -> str:
        self.retrieves.append(query)
        return f"<memory-result>{query}</memory-result>"

    async def consolidate(self, **_: Any) -> dict[str, object]:
        self.consolidations += 1
        return {"episode_summary": "did work", "insights_minted": 1, "facts_updated": 0}

    async def rebuild_index(self, **_: Any) -> None:
        return None


def _configure_with(brain: Any, cfg: dict[str, Any] | None = None) -> None:
    """Install a spy/real brain directly into runtime state (bypass select)."""
    from arcagent.modules.memory.config import MemoryConfig

    _runtime._state_var.set(
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


# -- Hotfix: bind() survives a sibling asyncio.Task (task 36) ------------


async def test_bind_makes_state_visible_in_a_sibling_task(tmp_path: Path) -> None:
    """configure() only binds the CURRENT task's ContextVar — invisible to a
    BRAND NEW sibling task (exactly what SessionRouter.handle() spawns per
    turn: turn 1's task completes, turn 2 gets an independent task created
    later from a common ancestor, not a child of turn 1). bind() with the
    already-built _State object must re-apply it in the new task without
    rebuilding anything (cheap, idempotent)."""
    import asyncio

    built_state: _runtime._State | None = None

    async def turn_one() -> None:
        nonlocal built_state
        _runtime.configure(config={"brain": "none"}, workspace=tmp_path, agent_did=_DID)
        built_state = _runtime.state()

    # turn_one's task runs to completion and ENDS before turn_two's task is
    # created — turn_two is therefore NOT a child of turn_one, exactly like
    # SessionRouter.handle()'s per-turn asyncio.create_task() calls.
    await asyncio.create_task(turn_one())
    assert built_state is not None

    async def turn_two() -> object:
        with pytest.raises(RuntimeError, match="before runtime is configured"):
            _runtime.state()
        _runtime.bind(built_state)
        return _runtime.state()

    result = await asyncio.create_task(turn_two())
    assert result is built_state, "bind() must re-apply the SAME state object, no rebuild"


# -- Acceptance: memory-less (NullBrain) ---------------------------------


async def test_memory_less_is_silent_noop_and_writes_no_files(tmp_path: Path) -> None:
    """brain='none' -> NullBrain: hooks no-op, recall empty, ZERO memory files."""
    _runtime.configure(config={"brain": "none"}, workspace=tmp_path, agent_did=_DID)
    st = _runtime.state()
    assert isinstance(st.brain, NullBrain)
    assert st.active is False

    sections: dict[str, str] = {}
    await inject_recall(_ctx({"sections": sections, "query": "who owns payments"}))
    await capture_tool(_ctx({"tool": "bash", "result": "ok"}))
    await capture_respond(_ctx({"messages": [{"role": "assistant", "content": "hi"}]}))
    ran = await consolidate_poll_once()

    assert "recall" not in sections  # nothing injected
    assert ran is False
    assert not (tmp_path / "memory").exists(), "memory-less agent must write no files"


# -- Honesty: memory-disabled prompt note (NullBrain) --------------------


async def test_memory_less_injects_disabled_note(tmp_path: Path) -> None:
    """brain='none' -> a prompt note tells the model durable memory is off (F10)."""
    _runtime.configure(config={"brain": "none"}, workspace=tmp_path, agent_did=_DID)
    sections: dict[str, str] = {}
    await inject_memory_disabled_note(_ctx({"sections": sections}))
    assert "memory_status" in sections
    note = sections["memory_status"].lower()
    assert "disabled" in note
    assert "saved to memory" in note or "not claim" in note


async def test_active_brain_injects_no_disabled_note() -> None:
    """A live brain stays silent — no over-claim note when memory actually works (F10)."""
    _configure_with(_SpyBrain())
    sections: dict[str, str] = {}
    await inject_memory_disabled_note(_ctx({"sections": sections}))
    assert "memory_status" not in sections


# -- Acceptance: wired (real arcmemory ArcMemoryBrain) -------------------


async def test_wired_arcmemory_capture_and_recall_activate(tmp_path: Path) -> None:
    """brain='arcmemory' -> capture persists the raw stream; recall activates."""
    pytest.importorskip("arcmemory")
    _runtime.configure(config={"brain": "arcmemory"}, workspace=tmp_path, agent_did=_DID)
    st = _runtime.state()
    assert st.active is True
    assert type(st.brain).__name__ == "ArcMemoryBrain"

    await capture_respond(
        _ctx({"messages": [{"role": "assistant", "content": "Ada owns the payments service"}]})
    )
    # Fast capture persists the raw episodic stream (SQLite). The curated daily-notes
    # are a consolidation output, so a bare capture writes no glass-box daily-log file.
    assert (tmp_path / "memory" / "index.db").exists()
    assert not (tmp_path / "memory" / "daily-log").exists()

    sections: dict[str, str] = {}
    await inject_recall(_ctx({"sections": sections, "query": "who owns payments"}))
    # Degraded (no embedder) BM25+graph recall still returns the captured line.
    assert "recall" in sections
    assert "payments" in sections["recall"]


# -- Wiring behavior (spy brain) -----------------------------------------


async def test_recall_is_once_per_turn_across_spawn_double_assembly() -> None:
    """Two identical-query assembles (spawn) trigger a single retrieve (cache)."""
    spy = _SpyBrain()
    _configure_with(spy)
    s1: dict[str, str] = {}
    s2: dict[str, str] = {}
    await inject_recall(_ctx({"sections": s1, "query": "same task"}))
    await inject_recall(_ctx({"sections": s2, "query": "same task"}))
    assert len(spy.retrieves) == 1
    assert s1["recall"] == s2["recall"]


async def test_capture_hooks_call_brain_and_count_events() -> None:
    spy = _SpyBrain()
    _configure_with(spy)
    await capture_tool(_ctx({"tool": "read", "result": "file contents"}))
    await capture_respond(_ctx({"messages": [{"role": "assistant", "content": "done"}]}))
    assert len(spy.captures) == 2
    assert _runtime.state().events_since_consolidate == 2


async def test_memory_search_tool_returns_boundary_marked() -> None:
    spy = _SpyBrain()
    _configure_with(spy)
    out = await memory_search("find the owner")
    assert "<memory-result>" in out
    assert spy.retrieves == ["find the owner"]


async def test_memory_search_tool_off_when_memory_less() -> None:
    _configure_with(NullBrain())
    out = await memory_search("anything")
    assert "not enabled" in out.lower()


# -- ACL priority-10 veto fires BEFORE the Brain call (T-083) ------------


class _VetoBus:
    """Stands in for the module bus with memory_acl vetoing at priority 10."""

    def __init__(self) -> None:
        self.events: list[str] = []

    async def emit(self, event: str, data: dict[str, Any], agent_did: str = "") -> SimpleNamespace:
        self.events.append(event)
        return SimpleNamespace(is_vetoed=True, veto_reason="ACL denied")


class _AllowBus:
    def __init__(self) -> None:
        self.events: list[str] = []

    async def emit(self, event: str, data: dict[str, Any], agent_did: str = "") -> SimpleNamespace:
        self.events.append(event)
        return SimpleNamespace(is_vetoed=False, veto_reason="")


async def test_acl_veto_blocks_recall_before_retrieve() -> None:
    spy = _SpyBrain()
    _configure_with(spy)
    st = _runtime.state()
    st.bus = _VetoBus()
    sections: dict[str, str] = {}
    await inject_recall(_ctx({"sections": sections, "query": "secret query"}))
    assert spy.retrieves == []  # ACL fired first — brain never consulted
    assert "recall" not in sections
    assert st.bus.events == ["memory.search"]


async def test_acl_veto_blocks_capture_before_brain() -> None:
    spy = _SpyBrain()
    _configure_with(spy)
    st = _runtime.state()
    st.bus = _VetoBus()
    await capture_respond(_ctx({"messages": [{"role": "assistant", "content": "secret"}]}))
    assert spy.captures == []  # capture vetoed before Brain.capture
    assert st.bus.events == ["memory.write"]


async def test_acl_allow_emits_search_then_retrieves() -> None:
    spy = _SpyBrain()
    _configure_with(spy)
    st = _runtime.state()
    st.bus = _AllowBus()
    sections: dict[str, str] = {}
    await inject_recall(_ctx({"sections": sections, "query": "ok query"}))
    assert st.bus.events == ["memory.search"]
    assert spy.retrieves == ["ok query"]
    assert "recall" in sections


# -- Consolidation trigger (fake clock + event counter, T-082) -----------


async def test_consolidate_fires_on_event_threshold() -> None:
    spy = _SpyBrain()
    _configure_with(spy, {"consolidate_event_threshold": 3})
    st = _runtime.state()
    st.events_since_consolidate = 2
    assert await consolidate_poll_once(now=st.last_activity) is False  # below threshold
    st.events_since_consolidate = 3
    assert await consolidate_poll_once(now=st.last_activity) is True  # threshold hit
    assert spy.consolidations == 1
    assert st.events_since_consolidate == 0  # reset after run


async def test_consolidate_fires_on_idle() -> None:
    spy = _SpyBrain()
    _configure_with(spy, {"consolidate_event_threshold": 100, "consolidate_idle_seconds": 60.0})
    st = _runtime.state()
    st.events_since_consolidate = 1
    st.last_activity = 0.0
    # Not yet idle enough.
    assert await consolidate_poll_once(now=30.0) is False
    # Idle past the gap -> fires.
    assert await consolidate_poll_once(now=120.0) is True
    assert spy.consolidations == 1


async def test_consolidate_noop_when_no_events() -> None:
    spy = _SpyBrain()
    _configure_with(spy, {"consolidate_event_threshold": 1})
    assert await consolidate_poll_once() is False
    assert spy.consolidations == 0
