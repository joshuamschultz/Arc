"""Bridge a schedule's ``deliver_to`` string to a real channel send.

The scheduler (in arcagent) records where a fired schedule's output should go
as a plain ``"platform:chat_id[:thread_id]"`` string — it cannot import the
gateway's :class:`DeliveryTarget` (arcagent must not depend on arcgateway).

This module lives on the gateway side of that seam. It parses the string into
a :class:`DeliveryTarget` and calls :meth:`SessionRouter.send`, which routes to
the platform adapter registered for that platform. The composition root
(``arc ui start`` / the embedded gateway) hands the returned closure to each
agent via ``ArcAgent.set_channel_deliver_fn`` before startup.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from arcgateway.delivery import DeliveryTarget
from arcgateway.session import SessionRouter

_logger = logging.getLogger("arcgateway.channel_delivery")

ChannelDeliverFn = Callable[[str, str], Awaitable[None]]

# The delivering agent's DID, either eagerly (the gateway's per-session factory
# already knows it) or as a callable resolved at send time (the always-on fleet
# must wire delivery BEFORE ArcAgent.startup(), which is where the DID is
# materialised — snapshotting it there captures "").
AgentDidSource = str | Callable[[], str]


def make_channel_deliver_fn(
    session_router: SessionRouter,
    agent_did: AgentDidSource = "",
) -> ChannelDeliverFn:
    """Return a ``(target_str, message) -> None`` closure over ``session_router``.

    ``agent_did`` binds the closure to the delivering agent so a fired schedule
    or ``notify_user`` goes out through THAT agent's bot when several bots serve
    the platform (one Telegram bot per agent). Bind one closure per agent.

    A malformed target string is logged and dropped rather than raised — the
    caller (the scheduler) treats delivery as fail-open so a bad ``deliver_to``
    never fails the run.
    """

    async def _deliver(target_str: str, message: str) -> None:
        try:
            target = DeliveryTarget.parse(target_str)
        except ValueError:
            _logger.warning("channel delivery: bad target %r — dropping", target_str)
            return
        did = agent_did() if callable(agent_did) else agent_did
        await session_router.send(target, message, agent_did=did)

    return _deliver


__all__ = ["AgentDidSource", "ChannelDeliverFn", "make_channel_deliver_fn"]
