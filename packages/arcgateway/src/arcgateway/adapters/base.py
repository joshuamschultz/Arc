"""BasePlatformAdapter Protocol — the contract platform adapters must satisfy.

Reconnect-watcher and FailedAdapter state live in ``_reconnect.py``; this
module is the pure Protocol surface, kept minimal so it stays under the
SPEC-018 G1.6 arcgateway-core LOC budget.

Design (SDD §3.1 Adapter Lifecycle):
    - Each adapter owns its own background poll/socket task.
    - Adapters run inside an asyncio.TaskGroup so a crash in one never kills siblings.
    - GatewayRunner owns lifecycle: connect on startup, reconnect on failure
      (see ``_reconnect.reconnect_watcher``), disconnect on shutdown.

Adapter lifecycle states:
    CONNECTING → CONNECTED → DISCONNECTING → DISCONNECTED
                          ↘ FAILED → (reconnect watcher retries)
                                   → PERMANENTLY_FAILED (after 20 attempts)

Platform adapters are implemented in T1.7.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from arcgateway.delivery import DeliveryTarget


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
    """Platform identifier (e.g. "telegram", "slack"). Shared across bots of the
    same platform — combine with ``agent_did`` for a unique outbound key."""

    agent_did: str
    """DID of the agent this adapter's bot serves. The SessionRouter keys its
    outbound registry by (name, agent_did) so a reply returns through the RIGHT
    bot when several bots run on one platform (one per agent). "" for a single
    adapter that fronts every agent (e.g. web)."""

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

    async def send_with_id(
        self,
        target: DeliveryTarget,
        message: str,
    ) -> str | None:
        """Send a message and return its platform-assigned message ID.

        Default implementation calls send() and returns None. Adapters that
        support message IDs (Telegram, Slack) MUST override this method to
        return the real ID so StreamBridge can use it for edit/delete ops.

        Args:
            target: Parsed delivery address.
            message: Text to send.

        Returns:
            str: Platform message ID if the platform returns one.
            None: If the platform does not return a message ID.
        """
        await self.send(target, message)
        return None
