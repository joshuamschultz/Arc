"""ActivityView — tool-call activity panel for ArcTUI.

Subscribes to the arcrun event bus (tool.start, tool.end, turn.start,
turn.end, llm.call) and renders a live log of agent activity.

Design constraints:
- No direct coupling to ArcAgent internals; events arrive via the callback
  registered in ``ArcTUI._wire_event_bus``.
- Thread-safe: Textual widgets must only be mutated on the UI thread.
  ``ActivityView.handle_event`` posts to the Textual message queue via
  ``call_from_thread`` when invoked from the arcrun bridge.
- Max 200 rows retained in the panel (configurable) — oldest entries drop
  off the top to bound memory usage (ASI-08 cascading failure prevention).
"""

from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, ClassVar

from textual.app import ComposeResult
from textual.widgets import Label, Static

_logger = logging.getLogger("arctui.activity")

# Maximum number of activity entries kept in the display.
MAX_ACTIVITY_ROWS = 200


class ActivityKind(StrEnum):
    """Classification of an activity entry."""

    TOOL_START = "tool_start"
    TOOL_COMPLETE = "tool_complete"
    TOOL_ERROR = "tool_error"
    TURN_START = "turn_start"
    TURN_END = "turn_end"
    LLM_CALL = "llm_call"
    INFO = "info"


@dataclass
class ActivityEntry:
    """A single entry in the activity log.

    Attributes
    ----------
    kind:
        Classification used to pick the CSS class.
    text:
        One-line description of the event.
    """

    kind: ActivityKind
    text: str


def _kind_to_css(kind: ActivityKind) -> str:
    """Return the CSS class name for *kind*."""
    mapping: dict[ActivityKind, str] = {
        ActivityKind.TOOL_START: "activity-start",
        ActivityKind.TOOL_COMPLETE: "activity-complete",
        ActivityKind.TOOL_ERROR: "activity-error",
        ActivityKind.TURN_START: "activity-start",
        ActivityKind.TURN_END: "activity-complete",
        ActivityKind.LLM_CALL: "activity-start",
        ActivityKind.INFO: "activity-complete",
    }
    return mapping.get(kind, "activity-complete")


def _arcrun_event_to_entry(event_type: str, data: dict[str, object]) -> ActivityEntry | None:
    """Convert a raw arcrun event dict to an ActivityEntry.

    Returns None for event types we don't surface in the activity panel.
    """
    if event_type == "tool.start":
        name = str(data.get("tool_name", data.get("name", "?")))
        return ActivityEntry(ActivityKind.TOOL_START, f"  tool  {name}  starting…")
    if event_type == "tool.end":
        name = str(data.get("tool_name", data.get("name", "?")))
        ok = not data.get("error")
        kind = ActivityKind.TOOL_COMPLETE if ok else ActivityKind.TOOL_ERROR
        status = "done" if ok else f"error: {data.get('error', '')}"
        return ActivityEntry(kind, f"  tool  {name}  {status}")
    if event_type == "turn.start":
        n = data.get("turn", "?")
        return ActivityEntry(ActivityKind.TURN_START, f"turn {n}  ─────────────────")
    if event_type == "turn.end":
        n = data.get("turn", "?")
        return ActivityEntry(ActivityKind.TURN_END, f"turn {n}  complete")
    if event_type == "llm.call":
        model = data.get("model", "?")
        tokens = data.get("total_tokens", "?")
        return ActivityEntry(ActivityKind.LLM_CALL, f"  llm   {model}  {tokens} tok")
    return None


class ActivityView(Static):
    """Live tool-call activity panel.

    Public API
    ----------
    handle_event(event_type, data)
        Ingest a raw arcrun event dict.  May be called from any thread.
    add_entry(entry)
        Append a pre-built ActivityEntry.  Must be called from UI thread.
    clear()
        Remove all entries.
    """

    DEFAULT_CSS: ClassVar[str] = """
    ActivityView {
        height: 1fr;
        overflow-y: auto;
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._entries: deque[ActivityEntry] = deque(maxlen=MAX_ACTIVITY_ROWS)
        self._labels: deque[Label] = deque(maxlen=MAX_ACTIVITY_ROWS)

    def compose(self) -> ComposeResult:
        """Empty on first render; entries are mounted dynamically."""
        return
        yield

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def handle_event(self, event_type: str, data: dict[str, object]) -> None:
        """Ingest an arcrun event.

        Safe to call from any thread.  Dispatches to the Textual event
        loop via ``call_from_thread`` so the DOM is only mutated on the
        UI thread.
        """
        entry = _arcrun_event_to_entry(event_type, data)
        if entry is None:
            return
        # call_from_thread ensures we cross back to the UI thread safely.
        self.app.call_from_thread(self.add_entry, entry)

    def add_entry(self, entry: ActivityEntry) -> None:
        """Append *entry* to the activity log.  Must run on UI thread.

        When MAX_ACTIVITY_ROWS is reached the oldest label is removed from
        the DOM and the deque naturally evicts the oldest entry (maxlen).
        """
        if len(self._entries) == MAX_ACTIVITY_ROWS:
            # Deque is full; evict the oldest DOM node before appending.
            oldest_label = self._labels[0]  # peek before deque mutates
            try:
                oldest_label.remove()
            except Exception as exc:  # widget may already be detached
                _logger.debug("Failed to remove evicted activity label: %s", exc)

        self._entries.append(entry)
        label = Label(entry.text, classes=_kind_to_css(entry.kind))
        self._labels.append(label)
        self.mount(label)
        self.scroll_end(animate=False)

    def clear(self) -> None:
        """Remove all activity entries from the panel."""
        for label in list(self._labels):
            try:
                label.remove()
            except Exception as exc:  # widget may already be detached
                _logger.debug("Failed to remove activity label during clear: %s", exc)
        self._entries.clear()
        self._labels.clear()
