"""Bridge arcteam ``MessagingService`` route events to the dashboard bus.

The Team Chat tab in ArcUI is supposed to feel like a live group chat —
visitors should see ``intake → architect → redteam → approver`` messages
land as they happen, not on the next 4-second poll tick. arcteam already
fires ``ui_reporter.emit_team_event("message_route", {...})`` once per
routed target URI; this module is the receiver that turns those calls
into ``team_chat`` frames on ``arcgateway.dashboard_events.DashboardEventBus``.

Wiring:

- arcui ``create_app()`` constructs a ``TeamChatDashboardBridge`` once
  it knows both the ``messaging_service`` and the ``dashboard_bus``
  (the bus is created inside the gateway lifespan).
- The bridge is attached via ``MessagingService.set_ui_reporter(...)``.
- Each route fires ``emit_team_event``; the bridge schedules
  ``dashboard_bus.publish("team_chat", payload)`` on the running loop.

Dependency direction: arcui knows about both arcteam and arcgateway —
arcteam and arcgateway know about neither. The bridge lives in arcui
to preserve that layering.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

_logger = logging.getLogger("arcui.team_chat_bridge")

_TEAM_CHAT_TOPIC = "team_chat"


class TeamChatDashboardBridge:
    """Forwards arcteam route events to the dashboard event bus.

    The shape ``MessagingService`` calls is::

        ui_reporter.emit_team_event(event_type="message_route", data={...})

    We accept any duck-typed equivalent — only the ``message_route``
    event_type is consumed; others are dropped to keep the dashboard
    payload focused on routing activity.
    """

    def __init__(self, dashboard_bus: Any) -> None:
        self._bus = dashboard_bus
        # Pending publish tasks — strong refs so the loop doesn't GC
        # them between scheduling and completion.
        self._pending: set[asyncio.Task[None]] = set()

    def emit_team_event(self, *, event_type: str, data: dict[str, Any]) -> None:
        """Schedule a ``team_chat`` publish for each route event.

        Synchronous so it can be called from ``MessagingService.send``
        without imposing an ``await`` on the message-send hot path.
        Publishes happen on the running asyncio loop; with no running
        loop the call is dropped (logged), matching the fail-open
        contract on every other UI hook.
        """
        if event_type != "message_route":
            return
        if self._bus is None:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            _logger.debug(
                "team_chat_bridge: no running loop; dropping route event"
            )
            return
        coro = self._bus.publish(_TEAM_CHAT_TOPIC, dict(data))
        task = loop.create_task(coro)
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)
