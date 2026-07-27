"""Channel delivery for agent-initiated sends (fired schedules, notify_user).

The outbound registry is keyed by ``(platform, agent_did)``, so a delivery
closure must carry the DELIVERING agent's DID or the send is dropped whenever
more than one bot serves the platform. The fleet wires delivery BEFORE
``ArcAgent.startup()`` — the only moment the agent can bind it into
``agent:ready`` — and an agent has no DID until startup materialises its
identity. These tests pin that a late-bound DID source is resolved at SEND
time, not at bind time.

Two same-platform adapters are registered on purpose: with only one, the
router's single-bot fallback masks a wrong/empty DID entirely.
"""

from __future__ import annotations

from typing import Any

import pytest

from arcgateway.channel_delivery import make_channel_deliver_fn
from arcgateway.delivery import DeliveryTarget
from arcgateway.session import SessionRouter

_SALES_DID = "did:arc:local:executor/7e3e1a09"
_OTHER_DID = "did:arc:local:executor/c0bef560"


class _RecordingAdapter:
    """Minimal ``_AdapterProtocol`` that records what it was asked to send."""

    def __init__(self, name: str, agent_did: str) -> None:
        self.name = name
        self.agent_did = agent_did
        self.sent: list[tuple[str, str]] = []

    async def send(self, target: DeliveryTarget, message: str) -> None:
        self.sent.append((target.chat_id, message))


def _router_with_two_telegram_bots() -> tuple[SessionRouter, _RecordingAdapter, _RecordingAdapter]:
    router = SessionRouter(executor=None)
    sales = _RecordingAdapter("telegram", _SALES_DID)
    other = _RecordingAdapter("telegram", _OTHER_DID)
    router.register_adapter(sales)
    router.register_adapter(other)
    return router, sales, other


@pytest.mark.asyncio
async def test_callable_did_is_resolved_at_send_time() -> None:
    """A DID that only exists after startup still routes to that agent's bot.

    Mirrors the fleet: the closure is built while ``agent.did`` is still "",
    and the DID materialises later. Binding the empty string would drop the
    send; resolving at call time delivers it.
    """
    router, sales, other = _router_with_two_telegram_bots()
    did_holder = {"did": ""}  # agent not started yet — no identity

    deliver = make_channel_deliver_fn(router, lambda: did_holder["did"])

    did_holder["did"] = _SALES_DID  # ArcAgent.startup() materialised identity

    await deliver("telegram:8293394811", "daily focus report")

    assert sales.sent == [("8293394811", "daily focus report")]
    assert other.sent == []


@pytest.mark.asyncio
async def test_plain_string_did_still_routes() -> None:
    """The eager form (gateway agent factory) is unchanged."""
    router, sales, other = _router_with_two_telegram_bots()

    deliver = make_channel_deliver_fn(router, _SALES_DID)
    await deliver("telegram:8293394811", "hello")

    assert sales.sent == [("8293394811", "hello")]
    assert other.sent == []


@pytest.mark.asyncio
async def test_bad_target_is_dropped_not_raised(caplog: Any) -> None:
    """Delivery is fail-open: a malformed ``deliver_to`` never fails the run."""
    router, sales, other = _router_with_two_telegram_bots()

    deliver = make_channel_deliver_fn(router, lambda: _SALES_DID)
    await deliver("not-a-target", "hello")

    assert sales.sent == []
    assert other.sent == []
