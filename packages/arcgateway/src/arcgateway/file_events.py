"""In-process pub/sub for filesystem-change notifications.

This module is the dedicated event channel for :class:`FileChangeEvent`s
emitted by :mod:`arcgateway.fs_watcher`. arcui subscribes to the bus and
forwards events to browser clients via the existing ``/ws`` route.

Why not extend ``stream_bridge``?
    ``arcgateway.stream_bridge`` is the LLM-stream-to-platform-adapter delivery
    pipeline (Telegram/Slack message edits). It owns a different responsibility
    and lifecycle. A dedicated bus here preserves Pillar 2 (Modularity) and
    keeps both modules small enough to read cold.

Design
------
* Module-level :data:`default_bus` for the simple "one bus per process" case.
  Tests reset it via :func:`_reset_default_bus_for_tests`.
* :class:`FileEventBus` is a thin async fan-out: subscribe a coroutine
  function, emit, every subscriber gets the event. Emit is itself ``async``
  so listener exceptions are surfaced + swallowed at the bus boundary
  (Pillar 1 — caller never has to wrap emit in a try/except).
* Subscribers are stored in a list (deterministic order, mirrors subscription
  order). Unsubscribe is by-identity.

This bus is in-process by design (D-005 simplified: arcgateway and arcui live
in the same Python process today; cross-process delivery rides on the
existing arcui WS, not on this bus).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

Listener = Callable[["FileChangeEvent"], Awaitable[None]]


@dataclass
class FileChangeEvent:
    """A normalized file-change notification.

    Attributes:
        agent_id: The agent whose tree changed.
        event_type: Domain event name, e.g. ``"policy:bullets_updated"``,
            ``"config:updated"``, ``"memory:updated"``. Match exactly the
            strings the frontend listens for.
        path: Path relative to the agent root (forward slashes), e.g.
            ``"workspace/policy.md"``.
        payload: Optional precomputed payload (e.g. parsed bullets) — the
            watcher does the parsing once so every subscriber doesn't reparse.
    """

    agent_id: str
    event_type: str
    path: str
    payload: dict[str, Any] = field(default_factory=dict)


class FileEventBus:
    """Async pub/sub for :class:`FileChangeEvent`."""

    def __init__(self) -> None:
        self._listeners: list[Listener] = []

    def subscribe(self, listener: Listener) -> None:
        """Register a coroutine listener. Idempotent on identity."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener: Listener) -> None:
        """Remove a previously-subscribed listener. No-op if absent."""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    def subscriber_count(self) -> int:
        return len(self._listeners)

    async def emit(self, event: FileChangeEvent) -> None:
        """Fan out an event to every subscriber.

        Listener exceptions are logged at WARN and swallowed so that one
        broken subscriber cannot starve the others.
        """
        # Snapshot first — listeners may unsubscribe themselves while running.
        for listener in list(self._listeners):
            try:
                await listener(event)
            except Exception:
                logger.warning(
                    "FileEventBus: listener %r raised on event %s; swallowing",
                    listener,
                    event.event_type,
                    exc_info=True,
                )


# Module-level default bus. Most callers should use this rather than threading
# a bus instance through every layer. Tests reset it via the helper below.
default_bus = FileEventBus()


def _reset_default_bus_for_tests() -> None:
    """Replace :data:`default_bus` with a fresh instance. Test-only."""
    global default_bus
    default_bus = FileEventBus()
