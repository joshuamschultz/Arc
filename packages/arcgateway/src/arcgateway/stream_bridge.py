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
from collections.abc import AsyncIterator

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import Delta

_logger = logging.getLogger("arcgateway.stream_bridge")

EDIT_TOKEN_BUFFER_SIZE: int = 20
EDIT_INTERVAL_MS: float = 1500.0
FLOOD_STRIKE_LIMIT: int = 3
THINKING_PLACEHOLDER: str = "..."


class StreamBridge:
    """Bridges an AsyncIterator[Delta] stream to platform adapter delivery."""

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

        message_id = await self._send_placeholder(adapter, target)

        async for delta in deltas:
            if delta.is_final:
                break

            if delta.kind != "token":
                _logger.debug(
                    "StreamBridge: skipping non-token delta kind=%s", delta.kind
                )
                continue

            buffer.append(delta.content)
            accumulated_parts.append(delta.content)

            should_flush = self._should_flush(buffer, last_edit_at)
            if should_flush and not flood_disabled and message_id is not None:
                accumulated = "".join(accumulated_parts)
                success = await self._attempt_edit(
                    adapter, target, message_id, accumulated
                )
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

        if accumulated:
            await self._send_final(adapter, target, accumulated)
        else:
            _logger.debug("StreamBridge: no content to deliver (target=%s)", target)

    @staticmethod
    def _should_flush(buffer: list[str], last_edit_at: float) -> bool:
        if len(buffer) == 0:
            return False
        if len(buffer) >= EDIT_TOKEN_BUFFER_SIZE:
            return True
        elapsed_ms = (time.monotonic() - last_edit_at) * 1000
        return elapsed_ms >= EDIT_INTERVAL_MS

    @staticmethod
    async def _send_placeholder(adapter: object, target: DeliveryTarget) -> str | None:
        try:
            message_id: str | None = await adapter.send_with_id(  # type: ignore[attr-defined]
                target, THINKING_PLACEHOLDER
            )
            _audit(
                "gateway.message.sent",
                {"target": str(target), "placeholder": True},
            )
            return message_id
        except Exception as exc:
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
            await adapter.edit_message(target, message_id, text)  # type: ignore[attr-defined]
            return True
        except Exception as exc:
            _logger.debug(
                "StreamBridge: edit_message failed (target=%s message_id=%s): %s",
                target,
                message_id,
                exc,
            )
            return False

    @staticmethod
    async def _send_final(adapter: object, target: DeliveryTarget, text: str) -> None:
        try:
            await adapter.send(target, text)  # type: ignore[attr-defined]
            _audit(
                "gateway.message.final_sent",
                {"target": str(target), "text_len": len(text)},
            )
        except Exception as exc:
            _logger.error(
                "StreamBridge: final send failed (target=%s): %s", target, exc
            )
            raise


def _audit(event_name: str, data: dict[str, object]) -> None:
    from arcgateway.telemetry import emit_audit
    emit_audit(_logger, event_name, dict(data))
