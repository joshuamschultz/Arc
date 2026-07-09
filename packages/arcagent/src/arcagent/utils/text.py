"""Shared text utilities for chat-platform bots (telegram, slack).

``split_message`` and ``_user_facing_error`` were duplicated across the two
bot modules and had begun to drift; this module owns the single canonical
version each bot imports. Callers always pass their platform-specific
``max_length`` explicitly, so the default here only serves ad-hoc use.
"""

from __future__ import annotations

import re

# Sentence-ending punctuation for boundary detection.
_SENTENCE_END = re.compile(r"[.!?]\s")


def user_facing_error(exc: Exception) -> str:
    """Map exceptions to user-friendly error messages.

    Avoids leaking internal details while giving the user actionable
    information about what went wrong.
    """
    try:
        from arcllm.exceptions import ArcLLMAPIError
    except ImportError:
        return "Error processing your message. Please try again."

    if isinstance(exc, ArcLLMAPIError):
        if exc.status_code == 429:
            return (
                "I'm currently rate limited by the LLM provider. "
                "Please try again in a minute or two."
            )
        if exc.status_code in {500, 502, 503}:
            return "The LLM provider is temporarily unavailable. Please try again shortly."
        if exc.status_code == 400 and "content_filter" in exc.body.lower():
            return (
                "Your message was blocked by the content safety filter. "
                "Please rephrase and try again."
            )

    if isinstance(exc, TimeoutError):
        return "The request timed out. Please try again with a simpler message."

    return "Error processing your message. Please try again."


def split_message(text: str, max_length: int = 4096) -> list[str]:
    """Split text into chunks respecting natural boundaries.

    Priority order:
    1. Double-newline (paragraph boundary)
    2. Single newline
    3. Sentence boundary (. ! ?)
    4. Hard split at max_length

    Args:
        text: The text to split.
        max_length: Maximum characters per chunk. Callers pass the
            platform limit (Telegram 4096, Slack 4000).

    Returns:
        List of text chunks, each <= max_length characters.
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

        chunk = remaining[:max_length]

        # Try paragraph boundary (double-newline)
        split_pos = chunk.rfind("\n\n")
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 2 :]
            continue

        # Try single newline
        split_pos = chunk.rfind("\n")
        if split_pos > 0:
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos + 1 :]
            continue

        # Try sentence boundary — find last match without materializing all
        last_match = None
        for m in _SENTENCE_END.finditer(chunk):
            last_match = m
        if last_match is not None:
            split_pos = last_match.end() - 1  # Include punctuation, not space
            chunks.append(remaining[:split_pos])
            remaining = remaining[split_pos:].lstrip()
            continue

        # Hard split — no natural boundary found
        chunks.append(remaining[:max_length])
        remaining = remaining[max_length:]

    return chunks


__all__ = ["split_message", "user_facing_error"]
