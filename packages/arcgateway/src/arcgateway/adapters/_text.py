"""The gateway's message splitter — the one implementation (SPEC-065 REQ-310).

SPEC-025 review §arch-M-1 — Slack, Mattermost, and Telegram each had a
near-duplicate ``split_message`` implementation. This module owns the one
canonical version, and REQ-310 keeps it here: an adapter *declares* its
platform's limit (``max_message_chars``) and preferred boundaries
(``text_boundaries``) and the gateway does the splitting, so a fourth platform
cannot arrive with a fourth splitter that chunks differently.

The algorithm is: walk the input, on overflow find the rightmost
boundary character (or substring) in the current window, split there,
repeat. If no substring boundary is found, an optional ``final_boundary``
finder (e.g. Telegram's sentence-ending fallback) is tried before the
chunk is hard-cut at ``max_length``.
"""

from __future__ import annotations

import re
from collections.abc import Callable

#: Sentence-ending punctuation, the last resort before a hard cut.
_SENTENCE_END = re.compile(r"[.!?]\s")

#: Boundaries used when an adapter declares none.
_DEFAULT_BOUNDARIES: tuple[str, ...] = ("\n\n", "\n")


def last_sentence_boundary(window: str) -> int | None:
    """Return the index just past the last ``.!?`` in ``window`` (or None).

    The returned index keeps the punctuation in the left chunk; the splitter
    ``lstrip``-s the trailing whitespace from the remainder.
    """
    last: re.Match[str] | None = None
    for match in _SENTENCE_END.finditer(window):
        last = match
    if last is None:
        return None
    return last.end() - 1


def split_for_platform(adapter: object, text: str) -> list[str]:
    """Split ``text`` for whatever platform ``adapter`` fronts.

    The adapter contributes two *declarations* and no code: ``max_message_chars``
    (its platform's hard limit) and ``text_boundaries`` (where its users expect a
    break). An adapter that declares no limit is unbounded and gets one chunk.

    Args:
        adapter: The platform adapter the text is bound for.
        text: The reply to split.

    Returns:
        Chunks the platform will accept, in order.
    """
    if not text:
        return []
    limit = getattr(adapter, "max_message_chars", 0)
    if not isinstance(limit, int) or limit <= 0:
        return [text]
    boundaries = getattr(adapter, "text_boundaries", _DEFAULT_BOUNDARIES)
    return split_message(
        text, limit, boundaries=tuple(boundaries), final_boundary=last_sentence_boundary
    )


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
