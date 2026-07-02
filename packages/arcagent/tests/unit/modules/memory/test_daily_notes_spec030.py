"""SPEC-030 — ongoing daily notes (decoupled from compaction).

Tier 1 raw capture (no LLM, crash-safe), Tier 2b session-end consolidation,
Tier 3 lazy new-day rollup (idempotent, recallable), config gating, and the
decoupling from compaction.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcagent.core.config import EvalConfig
from arcagent.modules.memory import _runtime
from arcagent.modules.memory import capabilities as caps


@pytest.fixture(autouse=True)
def _reset_runtime() -> None:
    _runtime.reset()


@pytest.fixture
def ws(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _runtime.configure(
        workspace=workspace,
        eval_config=EvalConfig(),
        telemetry=MagicMock(),
        agent_name="test",
    )
    return workspace


def _mock_model(content: str = "- tidied fact") -> MagicMock:
    m = MagicMock()
    m.invoke = AsyncMock(return_value=SimpleNamespace(content=content))
    return m


def _pin_today(monkeypatch: pytest.MonkeyPatch, when: date) -> None:
    """Pin the UTC day-boundary the module keys on (file names + rollup logic)."""
    monkeypatch.setattr(caps, "_today", lambda: when)


def _turn(user: str, assistant: str) -> list[dict[str, Any]]:
    return [{"role": "user", "content": user}, {"role": "assistant", "content": assistant}]


# --- REQ-001: decoupled from compaction -----------------------------------


def test_no_compaction_coupling() -> None:
    # Behavioral coverage lives in test_memory_capabilities.py (hook registry);
    # here we just assert the hook function is gone from the module surface.
    assert not hasattr(caps, "memory_pre_compaction")


# --- REQ-002: Tier 1 raw append, no LLM, crash-safe -----------------------


class TestRawCapture:
    async def test_raw_line_appended_without_llm(self, ws: Path, monkeypatch) -> None:
        _pin_today(monkeypatch, date(2026, 7, 2))
        st = _runtime.state()
        st.eval_model = _mock_model()

        await caps.memory_post_respond(SimpleNamespace(data={"messages": _turn("hi there", "hello back")}))

        note = (ws / "notes" / "2026-07-02.md").read_text(encoding="utf-8")
        assert "user: hi there → assistant: hello back" in note
        st.eval_model.invoke.assert_not_called()  # capture path never calls the model

    async def test_line_survives_without_background_loop(self, ws: Path, monkeypatch) -> None:
        _pin_today(monkeypatch, date(2026, 7, 2))
        # Never start entity_extraction_loop — the raw line must already be durable.
        await caps.memory_post_respond(SimpleNamespace(data={"messages": _turn("q", "a")}))
        assert "user: q → assistant: a" in (ws / "notes" / "2026-07-02.md").read_text()

    async def test_disabled_by_config(self, ws: Path, monkeypatch) -> None:
        _pin_today(monkeypatch, date(2026, 7, 2))
        _runtime.state().config.raw_capture_enabled = False
        await caps.memory_post_respond(SimpleNamespace(data={"messages": _turn("q", "a")}))
        # File still ensured, but no raw turn line.
        assert "user: q" not in (ws / "notes" / "2026-07-02.md").read_text()


# --- REQ-004/007: Tier 2b session-end consolidation -----------------------


class TestSessionConsolidation:
    async def test_one_model_pass_sanitized(self, ws: Path, monkeypatch) -> None:
        _pin_today(monkeypatch, date(2026, 7, 2))
        st = _runtime.state()
        (ws / "notes").mkdir()
        (ws / "notes" / "2026-07-02.md").write_text("# Daily Notes\n\n- a\n- a\n- b\n")
        st.eval_model = _mock_model("- a\n- b​\x07")  # includes zero-width + control char
        st.hybrid_search = MagicMock()
        st.hybrid_search.close = AsyncMock()

        await caps.memory_shutdown(SimpleNamespace(data={}))

        st.eval_model.invoke.assert_awaited_once()
        out = (ws / "notes" / "2026-07-02.md").read_text()
        assert "​" not in out and "\x07" not in out  # sanitized
        assert "- b" in out

    async def test_fail_open_keeps_raw(self, ws: Path, monkeypatch) -> None:
        _pin_today(monkeypatch, date(2026, 7, 2))
        st = _runtime.state()
        (ws / "notes").mkdir()
        raw = "# Daily Notes\n\n- raw kept\n"
        (ws / "notes" / "2026-07-02.md").write_text(raw)
        st.eval_model = MagicMock()
        st.eval_model.invoke = AsyncMock(side_effect=RuntimeError("model down"))
        st.hybrid_search = MagicMock()
        st.hybrid_search.close = AsyncMock()

        await caps.memory_shutdown(SimpleNamespace(data={}))  # must not raise
        assert "- raw kept" in (ws / "notes" / "2026-07-02.md").read_text()

    async def test_disabled_by_config(self, ws: Path, monkeypatch) -> None:
        _pin_today(monkeypatch, date(2026, 7, 2))
        st = _runtime.state()
        st.config.session_consolidation_enabled = False
        (ws / "notes").mkdir()
        (ws / "notes" / "2026-07-02.md").write_text("# Daily Notes\n\n- x\n")
        st.eval_model = _mock_model()
        st.hybrid_search = MagicMock()
        st.hybrid_search.close = AsyncMock()
        await caps.memory_shutdown(SimpleNamespace(data={}))
        st.eval_model.invoke.assert_not_called()


# --- REQ-005/006: Tier 3 lazy new-day rollup ------------------------------


class TestDayRollup:
    async def test_new_day_enqueues_one_rollup(self, ws: Path, monkeypatch) -> None:
        (ws / "notes").mkdir()
        (ws / "notes" / "2026-07-01.md").write_text("# yesterday\n\n- did X\n")
        _pin_today(monkeypatch, date(2026, 7, 2))
        spawned: list[Any] = []
        monkeypatch.setattr(
            caps, "spawn_background", lambda coro, **_: (spawned.append(coro), coro.close())
        )

        caps._ensure_daily_notes()  # first new-day file creation

        assert len(spawned) == 1  # exactly one prior day enqueued

    async def test_rollup_recallable_and_marks_rolled(self, ws: Path, monkeypatch) -> None:
        # Roll up an OLDER day (not today/yesterday) so it surfaces under the
        # Long-term section — today/yesterday are excluded from Long-term to
        # avoid double-counting what ### Today / ### Yesterday already show.
        _pin_today(monkeypatch, date(2026, 7, 2))
        st = _runtime.state()
        (ws / "notes").mkdir()
        prev = ws / "notes" / "2026-06-28.md"
        prev.write_text("# older\n\n- shipped the widget\n")
        st.eval_model = _mock_model("- 2026-06-28: shipped the widget")

        await caps._rollup_previous_day(prev)

        assert (ws / "notes" / "2026-06-28.rolled").exists()  # marked
        longterm = (ws / "notes" / "_longterm.md").read_text()
        assert "shipped the widget" in longterm
        # recallable via get_recent_notes under the Long-term section
        recalled = await st.notes.get_recent_notes()
        assert "Long-term" in recalled and "shipped the widget" in recalled

    async def test_idempotent_second_call_noop(self, ws: Path, monkeypatch) -> None:
        _pin_today(monkeypatch, date(2026, 7, 2))
        st = _runtime.state()
        (ws / "notes").mkdir()
        prev = ws / "notes" / "2026-07-01.md"
        prev.write_text("# y\n\n- fact\n")
        st.eval_model = _mock_model("- fact")

        await caps._rollup_previous_day(prev)
        await caps._rollup_previous_day(prev)  # marker present → no-op

        longterm = (ws / "notes" / "_longterm.md").read_text()
        assert longterm.count("## 2026-07-01") == 1  # not duplicated

    async def test_crash_before_marker_retried_once(self, ws: Path, monkeypatch) -> None:
        _pin_today(monkeypatch, date(2026, 7, 2))
        st = _runtime.state()
        (ws / "notes").mkdir()
        prev = ws / "notes" / "2026-07-01.md"
        prev.write_text("# y\n\n- fact\n")
        st.eval_model = MagicMock()
        st.eval_model.invoke = AsyncMock(side_effect=RuntimeError("boom"))

        with pytest.raises(RuntimeError):
            await caps._rollup_previous_day(prev)
        assert not (ws / "notes" / "2026-07-01.rolled").exists()  # no marker → eligible

        st.eval_model.invoke = AsyncMock(return_value=SimpleNamespace(content="- fact"))
        await caps._rollup_previous_day(prev)  # retry succeeds
        assert (ws / "notes" / "2026-07-01.rolled").exists()
        assert (ws / "notes" / "_longterm.md").read_text().count("## 2026-07-01") == 1

    async def test_crash_between_longterm_write_and_marker_no_duplicate(
        self, ws: Path, monkeypatch
    ) -> None:
        # Simulate a prior partial run: the ## <date> section is already in
        # _longterm.md but the marker was never written (crash in that window).
        _pin_today(monkeypatch, date(2026, 7, 2))
        st = _runtime.state()
        (ws / "notes").mkdir()
        prev = ws / "notes" / "2026-07-01.md"
        prev.write_text("# y\n\n- fact\n")
        (ws / "notes" / "_longterm.md").write_text("## 2026-07-01\n\n- fact\n")
        assert not (ws / "notes" / "2026-07-01.rolled").exists()  # no marker → retried
        st.eval_model = MagicMock()
        st.eval_model.invoke = AsyncMock(return_value=SimpleNamespace(content="- fact"))

        await caps._rollup_previous_day(prev)  # retry

        assert (ws / "notes" / "2026-07-01.rolled").exists()
        # upsert by date — the section is replaced, not duplicated
        assert (ws / "notes" / "_longterm.md").read_text().count("## 2026-07-01") == 1

    async def test_unrolled_skips_rolled_and_nondated(self, ws: Path, monkeypatch) -> None:
        notes = ws / "notes"
        notes.mkdir()
        (notes / "2026-06-29.md").write_text("a")
        (notes / "2026-06-30.md").write_text("b")
        (notes / "2026-06-30.rolled").write_text("")  # already rolled
        (notes / "_longterm.md").write_text("lt")  # non-dated, ignored
        prior = caps._unrolled_prior_days(notes, date(2026, 7, 2))
        assert prior == [notes / "2026-06-29.md"]  # 06-30 skipped (rolled), _longterm skipped

    async def test_full_backlog_drained_oldest_first(self, ws: Path, monkeypatch) -> None:
        # Two un-rolled prior days must BOTH be enqueued — no starvation.
        notes = ws / "notes"
        notes.mkdir()
        (notes / "2026-06-29.md").write_text("a")
        (notes / "2026-06-30.md").write_text("b")
        prior = caps._unrolled_prior_days(notes, date(2026, 7, 2))
        assert prior == [notes / "2026-06-29.md", notes / "2026-06-30.md"]  # oldest-first, all

    async def test_disabled_by_config(self, ws: Path, monkeypatch) -> None:
        (ws / "notes").mkdir()
        (ws / "notes" / "2026-07-01.md").write_text("- x")
        _pin_today(monkeypatch, date(2026, 7, 2))
        _runtime.state().config.daily_rollup_enabled = False
        spawned: list[Any] = []
        monkeypatch.setattr(caps, "spawn_background", lambda coro, **_: spawned.append(coro))
        caps._ensure_daily_notes()
        assert spawned == []
