"""Reconnect watcher + FailedAdapter state — implementation helpers.

Extracted from ``arcgateway.adapters.base`` to keep ``base.py`` as the pure
Protocol surface (SPEC-018 G1.6 LOC budget). The Protocol is the contract;
this module is the implementation that GatewayRunner uses to retry failed
adapters with exponential backoff.

Backoff formula
---------------
    min(30 * 2**(n-1), 300) seconds, capped at 5 min, max 20 attempts.
    n=1 → 30s, n=2 → 60s, n=3 → 120s, n=4 → 240s, n=5+ → 300s

After 20 failed attempts the adapter is marked permanently failed and a
FATAL audit event is emitted (TODO: emission wired in M1 integration).
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging

from arcgateway.adapters._backoff import exponential_backoff
from arcgateway.adapters.base import BasePlatformAdapter

_logger = logging.getLogger("arcgateway.adapters.base")

# Reconnect backoff parameters (Hermes pattern — see SDD §3.1).
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

        Returns:
            Seconds to wait before the next reconnect attempt.
        """
        return exponential_backoff(
            self.attempt, base=_BACKOFF_BASE_SECONDS, factor=2, cap=_BACKOFF_MAX_SECONDS
        )


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

        # Snapshot keys — we may modify failed_adapters during iteration.
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
                # TODO (M1 integration): emit gateway.adapter.fail audit event via telemetry.
                continue

            backoff = entry.next_backoff_seconds()
            _logger.info(
                "reconnect_watcher: adapter %r attempt %d/%d backoff=%.0fs",
                name,
                entry.attempt + 1,
                _MAX_RECONNECT_ATTEMPTS,
                backoff,
            )

            adapter = adapter_factory.get(name)
            if adapter is None:
                _logger.warning("reconnect_watcher: no adapter instance for %r; skipping.", name)
                continue

            entry.attempt += 1
            try:
                # Tear down first: an adapter whose event-source loop died may
                # still hold platform resources (Telegram's updater keeps polling
                # even after our keepalive task ends). Reconnecting without this
                # leaves an orphaned poller → a fresh getUpdates conflict. base.py
                # documents disconnect() as "called before reconnect"; honor it.
                await adapter.disconnect()
                await adapter.connect()
                _logger.info("reconnect_watcher: adapter %r reconnected successfully.", name)
                failed_adapters.pop(name, None)
            except Exception as exc:  # reason: fail-open — log + continue
                entry.last_error = exc
                _logger.warning(
                    "reconnect_watcher: adapter %r reconnect failed (attempt %d): %s",
                    name,
                    entry.attempt,
                    exc,
                )
