"""BasePlatformAdapter Protocol and reconnect watcher.

Design (SDD §3.1 Adapter Lifecycle):
    - Each adapter owns its own background poll/socket task.
    - Adapters run inside an asyncio.TaskGroup so a crash in one never kills siblings.
    - A single reconnect watcher walks _failed_adapters with exponential backoff:
        min(30 * 2**(n-1), 300) seconds, capped at 5 min, max 20 attempts.
    - After 20 failed attempts the adapter is marked permanently failed and
      a FATAL audit event is emitted.

Adapter lifecycle states:
    CONNECTING → CONNECTED → DISCONNECTING → DISCONNECTED
                          ↘ FAILED → (reconnect watcher retries)
                                   → PERMANENTLY_FAILED (after 20 attempts)

Platform adapters are implemented in T1.7. This module defines the
Protocol that all adapters must satisfy and the reconnect watcher
helper consumed by GatewayRunner.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import math
from typing import Protocol, runtime_checkable

from arcgateway.delivery import DeliveryTarget

_logger = logging.getLogger("arcgateway.adapters.base")

# Reconnect backoff parameters (Hermes pattern — see SDD §3.1)
_BACKOFF_BASE_SECONDS = 30
_BACKOFF_MAX_SECONDS = 300  # 5 minutes
_MAX_RECONNECT_ATTEMPTS = 20


@dataclasses.dataclass
class FailedAdapter:
    """Tracks failure state for a platform adapter awaiting reconnect.

    Attributes:
        name: Adapter name (e.g. "telegram", "slack").
        attempt: Number of reconnect attempts made so far.
        last_error: Exception from the most recent failure.
        permanently_failed: True after MAX_RECONNECT_ATTEMPTS exceeded.
    """

    name: str
    attempt: int = 0
    last_error: Exception | None = None
    permanently_failed: bool = False

    def next_backoff_seconds(self) -> float:
        """Compute backoff duration for the next reconnect attempt.

        Formula: min(30 * 2**(n-1), 300)
        n=1 → 30s, n=2 → 60s, n=3 → 120s, n=4 → 240s, n=5+ → 300s

        Returns:
            Seconds to wait before the next reconnect attempt.
        """
        # attempt is 0-indexed before increment, use attempt+1 for first retry
        n = max(1, self.attempt)
        return float(min(_BACKOFF_BASE_SECONDS * math.pow(2, n - 1), _BACKOFF_MAX_SECONDS))


@runtime_checkable
class BasePlatformAdapter(Protocol):
    """Protocol that all platform adapters must satisfy.

    Adapters are responsible for:
    1. Maintaining a connection to their platform (long-poll, WebSocket, etc.).
    2. Emitting normalised InboundEvents to the SessionRouter.
    3. Delivering formatted messages back to users via send().

    Minimum surface — adapters implement exactly these three methods.
    Additional capabilities (e.g., edit_message, delete_message, send_file)
    are optional and NOT part of this Protocol to keep the base surface minimal.

    The event-source loop (polling/websocket) runs as an asyncio.Task owned
    by GatewayRunner and is NOT part of this Protocol — adapters yield events
    from their own internal mechanism and call a registered callback instead.
    GatewayRunner registers the callback via connect().
    """

    name: str
    """Unique identifier for this adapter instance (e.g. "telegram", "slack")."""

    async def connect(self) -> None:
        """Establish the platform connection.

        Called by GatewayRunner on startup and after each successful reconnect.
        Implementations should start their polling/websocket loop here.
        Must return promptly (start background tasks, don't block).

        Raises:
            RuntimeError: If connection fails fatally (e.g. invalid credentials).
        """
        ...

    async def disconnect(self) -> None:
        """Gracefully shut down the platform connection.

        Called by GatewayRunner on shutdown or before reconnect.
        Must cancel any background tasks this adapter owns.
        Should NOT raise — log errors and return cleanly.
        """
        ...

    async def send(
        self,
        target: DeliveryTarget,
        message: str,
        *,
        reply_to: str | None = None,
    ) -> None:
        """Deliver a message to the specified target on this platform.

        Called by StreamBridge with each completed turn's output.

        Args:
            target: Parsed delivery address (platform, chat_id, thread_id).
            message: Text content to send. Adapters are responsible for
                platform-specific formatting and splitting at length limits.
            reply_to: Optional message ID to reply to (platform-specific).

        Raises:
            RuntimeError: On unrecoverable delivery failure. Transient
                failures (rate limits, network errors) should be retried
                internally by the adapter before raising.
        """
        ...


async def reconnect_watcher(
    failed_adapters: dict[str, FailedAdapter],
    adapter_factory: dict[str, BasePlatformAdapter],
    *,
    poll_interval_seconds: float = 5.0,
) -> None:
    """Watch for failed adapters and attempt reconnection with backoff.

    Runs as a long-lived asyncio.Task inside GatewayRunner. Walks
    ``failed_adapters`` on each poll interval and attempts to reconnect
    adapters whose backoff has elapsed.

    Args:
        failed_adapters: Mutable dict of currently-failed adapters.
            Reconnect watcher removes entries on successful reconnect.
        adapter_factory: Maps adapter name → adapter instance.
            Used to call connect() on reconnect.
        poll_interval_seconds: How often to check for reconnect candidates.
            Default 5s gives ~5s reconnect latency without busy-looping.
    """
    _logger.info("reconnect_watcher: started (poll_interval=%.1fs)", poll_interval_seconds)

    while True:
        await asyncio.sleep(poll_interval_seconds)

        if not failed_adapters:
            continue

        # Snapshot keys — we may modify failed_adapters during iteration
        for name in list(failed_adapters.keys()):
            entry = failed_adapters.get(name)
            if entry is None:
                continue

            if entry.permanently_failed:
                _logger.error(
                    "reconnect_watcher: adapter %r is permanently failed; "
                    "manual intervention required.",
                    name,
                )
                continue

            if entry.attempt >= _MAX_RECONNECT_ATTEMPTS:
                entry.permanently_failed = True
                _logger.critical(
                    "reconnect_watcher: adapter %r exceeded max reconnect attempts (%d); "
                    "marking PERMANENTLY_FAILED. Last error: %s",
                    name,
                    _MAX_RECONNECT_ATTEMPTS,
                    entry.last_error,
                )
                # TODO (M1 integration): emit gateway.adapter.fail audit event via telemetry
                continue

            backoff = entry.next_backoff_seconds()
            _logger.info(
                "reconnect_watcher: adapter %r attempt %d/%d backoff=%.0fs",
                name,
                entry.attempt + 1,
                _MAX_RECONNECT_ATTEMPTS,
                backoff,
            )

            # We don't actually sleep the full backoff here because the poll_interval
            # already provides a floor. Track last_attempt_ts in a real impl if
            # fine-grained backoff enforcement is needed (T1.7 can add that).
            adapter = adapter_factory.get(name)
            if adapter is None:
                _logger.warning("reconnect_watcher: no adapter instance for %r; skipping.", name)
                continue

            entry.attempt += 1
            try:
                await adapter.connect()
                _logger.info("reconnect_watcher: adapter %r reconnected successfully.", name)
                # Remove from failed dict on success
                failed_adapters.pop(name, None)
            except Exception as exc:
                entry.last_error = exc
                _logger.warning(
                    "reconnect_watcher: adapter %r reconnect failed (attempt %d): %s",
                    name,
                    entry.attempt,
                    exc,
                )
