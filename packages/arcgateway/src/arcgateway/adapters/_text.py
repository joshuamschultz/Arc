"""Shared text utilities for chat platform adapters.

SPEC-025 review §arch-M-1 — Slack, Mattermost, and Telegram each had a
near-duplicate ``split_message`` implementation. This module owns the one
canonical version. Each adapter calls in with its own platform-specific
limits and boundary preferences.

The algorithm is: walk the input, on overflow find the rightmost
boundary character (or substring) in the current window, split there,
repeat. If no substring boundary is found, an optional ``final_boundary``
finder (e.g. Telegram's sentence-ending fallback) is tried before the
chunk is hard-cut at ``max_length``.
"""

from __future__ import annotations

from collections.abc import Callable


def split_message(
    text: str,
    max_length: int,
    *,
    boundaries: tuple[str, ...] = ("\n\n", "\n"),
    final_boundary: Callable[[str], int | None] | None = None,
) -> list[str]:
    """Split ``text`` into chunks at the most preferred natural boundary.

    Args:
        text: The text to split. ``""`` returns ``[]``.
        max_length: Maximum characters per chunk. Chunks are guaranteed
            ``len(chunk) <= max_length``.
        boundaries: Preferred split points, in priority order. The first
            substring in this tuple that appears in the current window
            is used, and its separator is dropped from the output.
        final_boundary: Optional fallback tried only when no ``boundaries``
            substring matches. It receives the current window and returns
            the split index (kept in the left chunk) or ``None``. The
            remainder is ``lstrip``-ped — Telegram's sentence fallback keeps
            the ``.!?`` in the left chunk and drops the trailing whitespace.
            If it also returns ``None``, the chunk is hard-cut.

    Returns:
        List of chunks, each at most ``max_length`` characters. The
        concatenation of the chunks (with separators stripped) reproduces
        the input.
    """
    if not text:
        return []
    if len(text) <= max_length:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= max_length:
            chunks.append(remaining)
            break
        window = remaining[:max_length]
        for sep in boundaries:
            pos = window.rfind(sep)
            if pos > 0:
                chunks.append(remaining[:pos])
                remaining = remaining[pos + len(sep) :]
                break
        else:
            split_pos = final_boundary(window) if final_boundary is not None else None
            if split_pos is not None and split_pos > 0:
                chunks.append(remaining[:split_pos])
                remaining = remaining[split_pos:].lstrip()
            else:
                # No boundary in window — hard cut.
                chunks.append(remaining[:max_length])
                remaining = remaining[max_length:]
    return chunks
