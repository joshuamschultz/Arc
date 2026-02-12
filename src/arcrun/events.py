"""Event system. Synchronous inline emission."""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class Event:
    """Every action emits one. Generic with dict data."""

    type: str
    timestamp: float
    run_id: str
    data: dict[str, Any]


class EventBus:
    """Emits events, collects them, optionally calls handler."""

    def __init__(self, run_id: str, on_event: Callable[[Event], None] | None = None) -> None:
        self._run_id = run_id
        self._on_event = on_event
        self._events: list[Event] = []

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> Event:
        """Create event, append to log, call handler if set."""
        event = Event(
            type=event_type,
            timestamp=time.time(),
            run_id=self._run_id,
            data=data if data is not None else {},
        )
        self._events.append(event)
        if self._on_event is not None:
            self._on_event(event)
        return event

    @property
    def events(self) -> list[Event]:
        return list(self._events)
