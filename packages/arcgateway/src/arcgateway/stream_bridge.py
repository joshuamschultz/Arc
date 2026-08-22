"""StreamBridge -- LLM stream to platform adapter delivery with flood-control.

Design (SDD section 3.1 Stream Flood-Control):

    StreamBridge.consume() takes an AsyncIterator[Delta] produced by an Executor
    and delivers the accumulated text to the target platform via the adapter.

    Performance (SPEC-018 Wave B1):
        - String accumulation uses list[str] + "".join() at flush boundaries
          instead of repeated str+= to avoid O(N^2) copy cost.
        - Per-edit gateway.message.edited audit events replaced by a single
          per-turn gateway.message.turn_summary at the end of consume().

    Audit events emitted per turn (SDD section 4.2):
        gateway.message.sent           -- initial placeholder sent
        gateway.message.flood_disabled -- 3-strikes fallback activated (WARN)
        gateway.message.final_sent     -- final delivery
        gateway.message.turn_summary   -- per-turn edit count summary
"""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import cast

from arcgateway.adapters._text import split_for_platform
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta

_logger = logging.getLogger("arcgateway.stream_bridge")

EDIT_TOKEN_BUFFER_SIZE: int = 20
EDIT_INTERVAL_MS: float = 1500.0
FLOOD_STRIKE_LIMIT: int = 3
THINKING_PLACEHOLDER: str = "..."


class StreamBridge:
    """Bridges an AsyncIterator[Delta] stream to platform adapter delivery.

    ``buffer_threshold`` (default :data:`EDIT_TOKEN_BUFFER_SIZE`) controls
    how many tokens accumulate before each progressive edit is flushed.
    The default is tuned for Slack/Telegram where each edit is a separate
    rate-limited API call — batching tokens avoids hitting flood limits.
    Setting it to ``0`` switches to unbuffered mode: every token delta
    triggers an immediate edit (and no time-based flush). Use this when
    the adapter is a transport that doesn't rate-limit per edit (FastAPI
    SSE writers, ``PythonAdapter``, the in-browser WebSocket bridge):
    the visitor sees real-time token output instead of 1.5-second
    batches.
    """

    def __init__(self, *, buffer_threshold: int = EDIT_TOKEN_BUFFER_SIZE) -> None:
        if buffer_threshold < 0:
            raise ValueError("buffer_threshold must be >= 0")
        self._buffer_threshold = buffer_threshold

    async def consume(
        self,
        deltas: AsyncIterator[Delta],
        target: DeliveryTarget,
        adapter: object,
    ) -> None:
        """Consume a delta stream and deliver to the platform adapter."""
        buffer: list[str] = []
        accumulated_parts: list[str] = []
        last_edit_at: float = time.monotonic()
        consecutive_edit_failures: int = 0
        flood_disabled: bool = False
        message_id: str | None = None
        edit_count: int = 0
        terminal = Delta(kind="done", is_final=True)

        # Progressive in-place editing is only possible when the adapter exposes
        # edit_message (Telegram, Slack, Mattermost). Send-only transports may
        # expose the optional send_delta() capability; those receive ordered
        # token and terminal frames instead of a placeholder/final duplicate.
        supports_edit = hasattr(adapter, "edit_message")
        send_delta = getattr(adapter, "send_delta", None)
        # ``MagicMock`` manufactures arbitrary attributes on getattr; only
        # treat a declared/attached method as a real transport capability.
        supports_incremental = callable(send_delta) and (
            callable(getattr(type(adapter), "send_delta", None))
            or "send_delta" in getattr(adapter, "__dict__", {})
        )
        incremental_sender = cast(
            "Callable[[DeliveryTarget, Delta], Awaitable[None]]", send_delta
        )
        if supports_edit:
            await self._maybe_send_typing(adapter, target)
            message_id = await self._send_placeholder(adapter, target)
        can_edit = supports_edit and message_id is not None

        async for delta in deltas:
            if delta.is_final:
                terminal = delta
                break

            if delta.kind != "token":
                _logger.debug("StreamBridge: skipping non-token delta kind=%s", delta.kind)
                continue

            buffer.append(delta.content)
            accumulated_parts.append(delta.content)

            if supports_incremental:
                # The adapter owns its bounded queue/backpressure policy. Do
                # not catch errors here: cancellation and queue pressure must
                # reach the session runner rather than silently losing a token.
                await incremental_sender(target, delta)
                continue

            if not can_edit or flood_disabled or message_id is None:
                continue

            if self._should_flush_now(buffer, last_edit_at):
                accumulated = "".join(accumulated_parts)
                success = await self._attempt_edit(adapter, target, message_id, accumulated)
                if success:
                    consecutive_edit_failures = 0
                    edit_count += 1
                    buffer.clear()
                    last_edit_at = time.monotonic()
                else:
                    consecutive_edit_failures += 1
                    _logger.warning(
                        "StreamBridge: edit failure %d/%d target=%s",
                        consecutive_edit_failures,
                        FLOOD_STRIKE_LIMIT,
                        target,
                    )
                    if consecutive_edit_failures >= FLOOD_STRIKE_LIMIT:
                        flood_disabled = True
                        _logger.warning(
                            "StreamBridge: flood-control activated -- "
                            "switching to final-send-only for this turn (target=%s)",
                            target,
                        )
                        _audit(
                            "gateway.message.flood_disabled",
                            {"target": str(target)},
                        )

        accumulated = "".join(accumulated_parts)

        if edit_count > 0 or flood_disabled:
            _audit(
                "gateway.message.turn_summary",
                {
                    "target": str(target),
                    "edit_count": edit_count,
                    "flood_disabled": flood_disabled,
                },
            )

        if supports_incremental:
            # The executor's done sentinel is consumed above. Re-emit exactly
            # one terminal frame through the send-only projection so consumers
            # can close their stream without inventing a second final message.
            await incremental_sender(target, terminal)
            _audit(
                "gateway.message.final_sent",
                {"target": str(target), "text_len": len(accumulated)},
            )
            return

        await self._deliver_final(
            adapter,
            target,
            accumulated,
            message_id=message_id,
            can_edit=can_edit and not flood_disabled,
            pending=bool(buffer),
        )

    def _should_flush_now(self, buffer: list[str], last_edit_at: float) -> bool:
        if len(buffer) == 0:
            return False
        # Unbuffered mode: every token gets its own edit. Skips the
        # time-based flush entirely — the caller already gets per-delta
        # delivery so there's no batching gap to close.
        if self._buffer_threshold == 0:
            return True
        if len(buffer) >= self._buffer_threshold:
            return True
        elapsed_ms = (time.monotonic() - last_edit_at) * 1000
        return elapsed_ms >= EDIT_INTERVAL_MS

    @staticmethod
    async def _maybe_send_typing(adapter: object, target: DeliveryTarget) -> None:
        """Show a typing indicator before the first content, if the adapter supports it.

        Optional capability — probed via ``hasattr`` so adapters without a
        platform typing API (web, Slack bots) are simply skipped. Never raises.
        """
        send_typing = getattr(adapter, "send_typing", None)
        if send_typing is None:
            return
        try:
            await send_typing(target)
        except Exception as exc:  # reason: fail-open — typing is cosmetic
            _logger.debug("StreamBridge: send_typing failed (target=%s): %s", target, exc)

    @staticmethod
    async def _send_placeholder(adapter: object, target: DeliveryTarget) -> str | None:
        try:
            message_id: str | None = await adapter.send_with_id(  # type: ignore[attr-defined]  # reason: adapter is a duck-typed platform adapter (Telegram/Slack); send_with_id is opt-in per platform
                target, THINKING_PLACEHOLDER
            )
            _audit(
                "gateway.message.sent",
                {"target": str(target), "placeholder": True},
            )
            return message_id
        except Exception as exc:  # reason: fail-open — log + continue
            _logger.warning(
                "StreamBridge: failed to send placeholder (target=%s): %s", target, exc
            )
            return None

    @staticmethod
    async def _attempt_edit(
        adapter: object,
        target: DeliveryTarget,
        message_id: str,
        text: str,
    ) -> bool:
        """Try edit_message(); return True on success, False on failure.

        Per-edit audit events removed in SPEC-018 Wave B1.
        """
        try:
            await adapter.edit_message(target, message_id, text)  # type: ignore[attr-defined]  # reason: adapter is a duck-typed platform adapter; edit_message is opt-in per platform
            return True
        except Exception as exc:  # reason: fail-open — log + continue
            _logger.debug(
                "StreamBridge: edit_message failed (target=%s message_id=%s): %s",
                target,
                message_id,
                exc,
            )
            return False

    async def _deliver_final(
        self,
        adapter: object,
        target: DeliveryTarget,
        text: str,
        *,
        message_id: str | None,
        can_edit: bool,
        pending: bool,
    ) -> None:
        """Deliver the completed turn exactly once.

        Edit-capable channels finalize the streamed placeholder in place — never
        a duplicate message. When the reply exceeds the platform's single-message
        limit, the placeholder is edited to the first chunk and the remaining
        chunks are sent as follow-up messages, so long replies split instead of
        truncating. Send-only channels, flood fallback, or a failed final edit
        fall through to a single ``send`` (the adapter splits internally).
        """
        if not text:
            _logger.debug("StreamBridge: no content to deliver (target=%s)", target)
            return

        if can_edit and message_id is not None:
            chunks = _split_for_platform(adapter, text)
            if await self._finalize_in_place(adapter, target, message_id, chunks, pending=pending):
                _audit(
                    "gateway.message.final_sent",
                    {"target": str(target), "text_len": len(text), "chunks": len(chunks)},
                )
                return

        await self._send_final(adapter, target, text)

    async def _finalize_in_place(
        self,
        adapter: object,
        target: DeliveryTarget,
        message_id: str,
        chunks: list[str],
        *,
        pending: bool,
    ) -> bool:
        """Complete the streamed message in place; return False to fall back to send.

        Single-chunk replies already shown by the last streaming edit need no
        further work. Otherwise the placeholder is edited to ``chunks[0]`` and any
        overflow chunks are delivered as new messages — never re-sending the first
        chunk, so the reply is never duplicated.
        """
        if len(chunks) == 1 and not pending:
            return True
        if not await self._attempt_edit(adapter, target, message_id, chunks[0]):
            return False
        for chunk in chunks[1:]:
            await adapter.send(target, chunk)  # type: ignore[attr-defined]  # reason: adapter is a duck-typed platform adapter; send is part of the minimum contract
        return True

    @staticmethod
    async def _send_final(adapter: object, target: DeliveryTarget, text: str) -> None:
        try:
            await adapter.send(target, text)  # type: ignore[attr-defined]  # reason: adapter is a duck-typed platform adapter; send is part of the minimum adapter contract
            _audit(
                "gateway.message.final_sent",
                {"target": str(target), "text_len": len(text)},
            )
        except Exception as exc:  # reason: re-raise after log
            _logger.error("StreamBridge: final send failed (target=%s): %s", target, exc)
            raise


def _split_for_platform(adapter: object, text: str) -> list[str]:
    """Split ``text`` into platform-sized chunks (SPEC-065 REQ-310).

    Splitting is the gateway's, so the bridge asks the shared splitter rather
    than each adapter. A length-limited platform declares ``max_message_chars``
    and its preferred ``text_boundaries``; one that declares neither (web,
    in-process, test doubles) is unbounded and gets a single chunk.
    """
    return split_for_platform(adapter, text) or [text]


def _audit(event_name: str, data: dict[str, object]) -> None:
    from arcgateway.telemetry import emit_audit

    emit_audit(_logger, event_name, dict(data))
