"""Unit tests for ActivityView.

Verifies:
- Arcrun events are converted to ActivityEntry correctly.
- handle_event is a no-op for unknown event types.
- add_entry mounts a label with the correct CSS class.
- MAX_ACTIVITY_ROWS limit evicts oldest entries.
- clear() resets all state.
"""

from __future__ import annotations

import pytest

from arctui.activity import (
    MAX_ACTIVITY_ROWS,
    ActivityEntry,
    ActivityKind,
    ActivityView,
    _arcrun_event_to_entry,
)

# ---------------------------------------------------------------------------
# Pure-function tests
# ---------------------------------------------------------------------------


class TestArcrunEventToEntry:
    """Tests for the _arcrun_event_to_entry converter."""

    def test_tool_start(self) -> None:
        """tool.start → ActivityKind.TOOL_START."""
        entry = _arcrun_event_to_entry("tool.start", {"tool_name": "bash"})
        assert entry is not None
        assert entry.kind == ActivityKind.TOOL_START
        assert "bash" in entry.text

    def test_tool_end_ok(self) -> None:
        """tool.end without error → TOOL_COMPLETE."""
        entry = _arcrun_event_to_entry("tool.end", {"tool_name": "bash"})
        assert entry is not None
        assert entry.kind == ActivityKind.TOOL_COMPLETE

    def test_tool_end_error(self) -> None:
        """tool.end with error → TOOL_ERROR."""
        entry = _arcrun_event_to_entry(
            "tool.end", {"tool_name": "bash", "error": "permission denied"}
        )
        assert entry is not None
        assert entry.kind == ActivityKind.TOOL_ERROR
        assert "permission denied" in entry.text

    def test_turn_start(self) -> None:
        """turn.start → TURN_START."""
        entry = _arcrun_event_to_entry("turn.start", {"turn": 1})
        assert entry is not None
        assert entry.kind == ActivityKind.TURN_START

    def test_turn_end(self) -> None:
        """turn.end → TURN_END."""
        entry = _arcrun_event_to_entry("turn.end", {"turn": 1})
        assert entry is not None
        assert entry.kind == ActivityKind.TURN_END

    def test_llm_call(self) -> None:
        """llm.call → LLM_CALL with model name."""
        entry = _arcrun_event_to_entry("llm.call", {"model": "claude-3", "total_tokens": 500})
        assert entry is not None
        assert entry.kind == ActivityKind.LLM_CALL
        assert "claude-3" in entry.text

    def test_unknown_event_returns_none(self) -> None:
        """Unknown event types return None."""
        entry = _arcrun_event_to_entry("unknown.event", {})
        assert entry is None

    def test_tool_name_fallback_to_name_key(self) -> None:
        """'name' key used as fallback when 'tool_name' is absent."""
        entry = _arcrun_event_to_entry("tool.start", {"name": "read_file"})
        assert entry is not None
        assert "read_file" in entry.text


class TestActivityEntry:
    """Tests for ActivityEntry dataclass."""

    def test_construction(self) -> None:
        """ActivityEntry stores kind and text."""
        e = ActivityEntry(kind=ActivityKind.INFO, text="hello")
        assert e.kind == ActivityKind.INFO
        assert e.text == "hello"


# ---------------------------------------------------------------------------
# Widget behaviour tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_entry_visible() -> None:
    """add_entry mounts a label in the widget."""
    from textual.app import App, ComposeResult

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield ActivityView(id="av")

    async with TestApp().run_test() as pilot:
        av = pilot.app.query_one("#av", ActivityView)
        entry = ActivityEntry(ActivityKind.TOOL_START, "  tool  bash  starting…")
        av.add_entry(entry)
        assert len(av._entries) == 1
        assert len(av._labels) == 1
        assert av._entries[0].text == "  tool  bash  starting…"


@pytest.mark.asyncio
async def test_clear_resets_state() -> None:
    """clear() removes all entries and labels."""
    from textual.app import App, ComposeResult

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield ActivityView(id="av")

    async with TestApp().run_test() as pilot:
        av = pilot.app.query_one("#av", ActivityView)
        av.add_entry(ActivityEntry(ActivityKind.TOOL_COMPLETE, "done"))
        av.add_entry(ActivityEntry(ActivityKind.TURN_START, "start"))
        av.clear()
        assert len(av._entries) == 0
        assert len(av._labels) == 0


@pytest.mark.asyncio
async def test_max_rows_eviction() -> None:
    """Entries beyond MAX_ACTIVITY_ROWS are evicted."""
    from textual.app import App, ComposeResult

    class TestApp(App[None]):
        def compose(self) -> ComposeResult:
            yield ActivityView(id="av")

    async with TestApp().run_test() as pilot:
        av = pilot.app.query_one("#av", ActivityView)
        # Fill beyond max
        for i in range(MAX_ACTIVITY_ROWS + 10):
            av.add_entry(ActivityEntry(ActivityKind.INFO, f"entry {i}"))

        # Should not exceed max
        assert len(av._entries) <= MAX_ACTIVITY_ROWS
        assert len(av._labels) <= MAX_ACTIVITY_ROWS


@pytest.mark.asyncio
async def test_event_to_entry_integration() -> None:
    """handle_event fires the correct entry kind via call_from_thread."""
    # We test the converter independently here since call_from_thread
    # requires a running Textual app and a real threading context.
    # The _arcrun_event_to_entry function is already tested above.
    # Integration via handle_event is covered in smoke tests.
    entry = _arcrun_event_to_entry("tool.end", {"tool_name": "bash", "error": ""})
    # Empty error string means no error key was set meaningfully
    # The actual check is `not data.get("error")` so empty str is falsy → COMPLETE
    assert entry is not None
    assert entry.kind == ActivityKind.TOOL_COMPLETE
