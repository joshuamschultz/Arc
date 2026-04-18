"""StreamBridge — LLM stream to platform adapter delivery with flood-control.

STUB: This module is a placeholder for T1.6+ implementation.

Design (SDD §3.1 Stream Flood-Control):
    StreamBridge consumes the AsyncIterator[Delta] produced by an Executor
    and delivers formatted output to the platform via Adapter.send().

    Flood-control (Hermes 3-strikes rule):
        After 3 consecutive edit failures (Telegram flood limit, Discord rate
        limit, Slack tier-3), permanently disable progressive edits for the
        rest of that turn and fall back to final-send-only delivery.

        Without this, one rate-limited channel stalls all concurrent sessions.

    Implementation in T1.6 will:
    1. Buffer incoming Delta(kind="token") chunks into a message buffer.
    2. On each buffer flush, attempt Adapter.edit_message() or send().
    3. Count consecutive failures. On 3rd failure, set final-only mode.
    4. In final-only mode, accumulate all tokens and send once at turn end.
    5. Always deliver a final message at turn end regardless of mode.

TODO T1.6: Implement full StreamBridge with flood-control.
See SDD §3.1 Stream Flood-Control for the complete design.
"""

from __future__ import annotations

import logging

_logger = logging.getLogger("arcgateway.stream_bridge")


class StreamBridge:
    """Bridges LLM streaming output to platform adapter delivery.

    STUB — Not implemented. See module docstring for full design.

    TODO T1.6: Implement flood-control (3-strikes), message buffering,
    progressive edit delivery, and final-send fallback.
    """

    pass
