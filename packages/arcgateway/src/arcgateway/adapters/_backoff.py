"""Shared exponential-backoff formula for chat platform adapters.

SPEC review §duplication-reuse — the reconnect watcher and the Telegram
network-retry path each hand-rolled the identical ``min(base * factor**(n-1),
cap)`` shape with their own tuned constants. This module owns the one
canonical formula; each caller passes its own ``base``/``factor``/``cap``.

Mattermost's ``_ws_loop`` is deliberately excluded: it carries backoff as a
mutable loop variable (``backoff *= factor``) with reset-on-connect, not a
pure function of an attempt counter, so it is a different shape. arcllm's
retry adds jitter and Retry-After handling and keeps its own policy.
"""

from __future__ import annotations


def exponential_backoff(attempt: int, *, base: float, factor: float, cap: float) -> float:
    """Compute capped exponential backoff for a 1-indexed retry attempt.

    Formula: ``min(base * factor**(attempt-1), cap)``.

    Args:
        attempt: 1-indexed attempt number. Values below 1 are floored to 1
            (the first attempt always waits ``base`` seconds).
        base: Delay for the first attempt.
        factor: Multiplier applied per attempt.
        cap: Maximum delay; the result never exceeds this.

    Returns:
        Seconds to wait before the given attempt.
    """
    n = max(1, attempt)
    return float(min(base * factor ** (n - 1), cap))
