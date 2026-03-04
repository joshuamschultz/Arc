"""SubscriptionManager — server-side event filtering for browser clients.

Browser clients send subscribe messages specifying which agents, layers,
and teams to receive. SubscriptionManager filters events before pushing
to each client's queue, reducing bandwidth for large fleets.
"""

from __future__ import annotations

import asyncio
import logging

from pydantic import BaseModel

from arcui.types import UIEvent
from arcui.ws_helpers import safe_enqueue

logger = logging.getLogger(__name__)


class Subscription(BaseModel):
    """Filter criteria for a browser client. None = receive all."""

    agents: list[str] | None = None
    layers: list[str] | None = None
    teams: list[str] | None = None


class SubscriptionManager:
    """Manages per-client subscriptions and filtered broadcast.

    Each browser client's queue is mapped to a Subscription. Events are
    only pushed to queues whose subscription matches the event.
    """

    def __init__(self) -> None:
        self._subscriptions: dict[asyncio.Queue[str], Subscription] = {}

    def set_subscription(
        self, queue: asyncio.Queue[str], subscription: Subscription
    ) -> None:
        """Set or update subscription for a client queue."""
        self._subscriptions[queue] = subscription

    def remove_subscription(self, queue: asyncio.Queue[str]) -> None:
        """Remove a client's subscription (e.g., on disconnect)."""
        self._subscriptions.pop(queue, None)

    def matches(self, queue: asyncio.Queue[str], event: UIEvent) -> bool:
        """Check if an event matches a queue's subscription.

        Unregistered queues default to matching everything (backward compat).
        """
        sub = self._subscriptions.get(queue)
        if sub is None:
            return True  # No subscription = receive all

        if sub.agents is not None and event.agent_id not in sub.agents:
            return False
        if sub.layers is not None and event.layer not in sub.layers:
            return False
        if sub.teams is not None:
            event_team = event.data.get("team")
            if event_team not in sub.teams:
                return False
        return True

    def broadcast_filtered(self, event: UIEvent) -> None:
        """Push event to all matching client queues.

        Serializes the event once, then puts the JSON string into each
        matching queue. Drops oldest on full queues.
        """
        message = event.model_dump_json()
        for queue, _sub in self._subscriptions.items():
            if self.matches(queue, event):
                safe_enqueue(queue, message)
