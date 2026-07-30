"""The policy module's own bounded record of what the agent's tools actually did.

``agent:post_respond`` carries only the user's text and the assistant's final
text, so the ACE Reflector was asked to improve tool-calling behavior while blind
to every tool call. This buffer accumulates the tool events the registry already
emits (``agent:pre_tool`` → name + args, ``agent:post_tool`` → result) and
renders them as message dicts the engine formats through its existing path.

Self-contained by design: policy never reads arcmemory (so it still works on an
agent with no memory module loaded), and never enriches the ``agent:post_respond``
payload (memory distills from that same payload and must keep seeing the
conversation only).

Bounded on three axes so a long session cannot accumulate an unbounded transcript
of tool output: entry count (FIFO eviction), argument text, and result text.
Truncation is announced in the rendered text rather than cutting silently — a
Reflector told "this was cut" reasons differently than one shown a lie.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# ~40 entries x ~2KB worst case keeps the buffer well inside the eval token
# budget even before ``PolicyEngine._chunk_for_budget`` splits it.
MAX_ENTRIES = 40
MAX_ARGS_CHARS = 500
MAX_RESULT_CHARS = 1500

# A pre_tool with no matching post_tool: the registry raises before emitting the
# post event, so an unclosed record IS the failure signal the Reflector needs.
_NO_RESULT = "(did not complete — the call raised, timed out, or was vetoed)"


def _clip(text: str, limit: int) -> str:
    """Truncate to ``limit`` chars, saying so in-band when anything was dropped."""
    if len(text) <= limit:
        return text
    return f"{text[:limit]} [truncated: showing first {limit} of {len(text)} chars]"


def _render_args(args: Any) -> str:
    """Serialize tool arguments compactly; never raise on an exotic value."""
    try:
        text = json.dumps(args, default=str, sort_keys=True)
    except (TypeError, ValueError):
        text = repr(args)
    return _clip(text, MAX_ARGS_CHARS)


@dataclass
class _Entry:
    """One tool call. ``result is None`` until the post_tool event closes it."""

    tool: str
    args: str
    result: str | None = None


@dataclass
class ToolActivity:
    """Bounded FIFO record of the tool calls made since the last policy eval."""

    entries: deque[_Entry] = field(default_factory=lambda: deque(maxlen=MAX_ENTRIES))

    def record_call(self, tool: str, args: Any) -> None:
        """Open a record on ``agent:pre_tool``."""
        self.entries.append(_Entry(tool=tool, args=_render_args(args)))

    def record_result(self, tool: str, result: Any) -> None:
        """Close the most recent open record for ``tool`` on ``agent:post_tool``.

        Newest-first matching pairs concurrent same-tool calls in LIFO order. If
        no open record exists (the pre_tool half was evicted, or the tool was
        dispatched before this module was configured) the result is kept anyway —
        a result without its arguments still teaches output handling.
        """
        clipped = _clip(str(result), MAX_RESULT_CHARS)
        for entry in reversed(self.entries):
            if entry.tool == tool and entry.result is None:
                entry.result = clipped
                return
        self.entries.append(_Entry(tool=tool, args="(not recorded)", result=clipped))

    def as_messages(self) -> list[dict[str, Any]]:
        """Render as ``role``/``content`` dicts — the shape the engine already formats."""
        return [
            {
                "role": "tool",
                "content": (
                    f"called `{e.tool}` with args {e.args} -> "
                    f"{_NO_RESULT if e.result is None else e.result}"
                ),
            }
            for e in self.entries
        ]

    def clear(self) -> None:
        """Drop every record — called once its content has been evaluated."""
        self.entries.clear()

    def to_json(self) -> list[dict[str, Any]]:
        """Serialize for the workspace state file."""
        return [{"tool": e.tool, "args": e.args, "result": e.result} for e in self.entries]

    @classmethod
    def from_json(cls, raw: Any) -> ToolActivity:
        """Rebuild from the state file; a missing or malformed value yields empty."""
        activity = cls()
        if not isinstance(raw, list):
            return activity
        for item in raw:
            if not isinstance(item, dict) or not isinstance(item.get("tool"), str):
                continue
            result = item.get("result")
            activity.entries.append(
                _Entry(
                    tool=item["tool"],
                    args=str(item.get("args", "")),
                    result=None if result is None else str(result),
                )
            )
        return activity


__all__ = ["MAX_ARGS_CHARS", "MAX_ENTRIES", "MAX_RESULT_CHARS", "ToolActivity"]
