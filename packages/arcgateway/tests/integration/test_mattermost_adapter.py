"""Integration tests -- MattermostAdapter against an in-process fake MM WS server.

NO docker dependency.  A minimal asyncio + aiohttp-based fake Mattermost
WebSocket server speaks the relevant subset of the MM WS protocol:

  - Accepts WS upgrade at ``/api/v4/websocket`` with ``Authorization: Bearer <token>``
  - Forwards JSON envelopes: ``{"event": "posted", "data": {"post": "<json>"}}``
  - Accepts REST POSTs at ``/api/v4/posts`` and records them

Tests verify:
  test_inbound_posted_event_dispatched
      Fake server pushes a "posted" WS event -> adapter's on_message called with
      correct InboundEvent (platform, session_key, message).

  test_audit_chain_carries_platform_mattermost
      After a successful REST POST, adapter emits gateway.message.delivered with
      platform="mattermost".

  test_send_posts_to_rest_api
      Adapter.send() -> drain task -> POST /api/v4/posts recorded by fake server.

  test_reconnect_on_transient_ws_close
      Fake server closes the WS; adapter reconnects within backoff window.

  test_federal_tier_rejects_non_intranet_url
      Adapter constructor raises ValueError for a public host at federal tier,
      never reaching the WS.

  test_disconnect_stops_ws_task
      After adapter.disconnect(), WS task is cancelled and no further events
      are dispatched.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import patch

import aiohttp
import aiohttp.web
import pytest

from arcgateway.adapters.mattermost import MattermostAdapter
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import InboundEvent

pytestmark = pytest.mark.asyncio

_logger = logging.getLogger(__name__)

_BOT_TOKEN = "mm-test-pat-integration-00000"


# ---------------------------------------------------------------------------
# In-process fake Mattermost server
# ---------------------------------------------------------------------------


class FakeMattermostServer:
    """Minimal async Mattermost-protocol fake for integration testing.

    Runs an aiohttp.web Application on a random port.  Exposes:
      - WS  /api/v4/websocket  -- accepts any Bearer token
      - POST /api/v4/posts     -- records the body

    Use ``push_event(envelope_dict)`` to push JSON to all connected WS clients.
    Read ``recorded_posts`` to inspect what the adapter POSTed.
    """

    def __init__(self) -> None:
        self._app = aiohttp.web.Application()
        self._app.router.add_route("GET", "/api/v4/websocket", self._ws_handler)
        self._app.router.add_route("POST", "/api/v4/posts", self._posts_handler)
        self._runner: aiohttp.web.AppRunner | None = None
        self._site: aiohttp.web.TCPSite | None = None
        self._ws_clients: list[aiohttp.web.WebSocketResponse] = []
        self.recorded_posts: list[dict[str, Any]] = []
        self.port: int = 0

    async def start(self) -> None:
        self._runner = aiohttp.web.AppRunner(self._app)
        await self._runner.setup()
        self._site = aiohttp.web.TCPSite(self._runner, "127.0.0.1", 0)
        await self._site.start()
        # Retrieve the actual bound port.
        assert self._runner.addresses
        self.port = self._runner.addresses[0][1]
        _logger.debug("FakeMattermostServer: listening on port %d", self.port)

    async def stop(self) -> None:
        # Close all open WS connections gracefully.
        for ws in list(self._ws_clients):
            try:
                await ws.close()
            except Exception:
                _logger.debug("FakeMattermostServer: error closing WS client")
        self._ws_clients.clear()
        if self._runner is not None:
            await self._runner.cleanup()

    async def push_event(self, envelope: dict[str, Any]) -> None:
        """Push a JSON envelope to all connected WS clients."""
        text = json.dumps(envelope)
        for ws in list(self._ws_clients):
            try:
                await ws.send_str(text)
            except Exception as exc:
                _logger.debug("FakeMattermostServer: push error: %s", exc)

    async def close_all_connections(self) -> None:
        """Force-close all WS connections to trigger adapter reconnect."""
        for ws in list(self._ws_clients):
            try:
                await ws.close(code=aiohttp.WSCloseCode.GOING_AWAY)
            except Exception:
                _logger.debug("FakeMattermostServer: error force-closing WS")
        self._ws_clients.clear()

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # -- handlers --

    async def _ws_handler(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.WebSocketResponse:
        ws = aiohttp.web.WebSocketResponse()
        await ws.prepare(request)
        self._ws_clients.append(ws)
        try:
            async for _msg in ws:
                pass  # ignore client->server frames in v1 protocol
        finally:
            if ws in self._ws_clients:
                self._ws_clients.remove(ws)
        return ws

    async def _posts_handler(
        self, request: aiohttp.web.Request
    ) -> aiohttp.web.Response:
        body = await request.json()
        self.recorded_posts.append(body)
        fake_post = {"id": f"srv-post-{len(self.recorded_posts)}", **body}
        return aiohttp.web.json_response(fake_post, status=201)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def fake_mm_server() -> AsyncGenerator[FakeMattermostServer, None]:
    server = FakeMattermostServer()
    await server.start()
    yield server
    await server.stop()


def _make_adapter_for_server(
    server: FakeMattermostServer,
    *,
    allowed_channel_ids: list[str] | None = None,
    bot_user_id: str = "bot-uid",
) -> tuple[MattermostAdapter, list[InboundEvent], list[tuple[str, dict[str, Any]]]]:
    received: list[InboundEvent] = []
    audit_events: list[tuple[str, dict[str, Any]]] = []

    async def _on_message(event: InboundEvent) -> None:
        received.append(event)

    adapter = MattermostAdapter(
        server_url=server.base_url,
        bot_token=_BOT_TOKEN,
        on_message=_on_message,
        allowed_channel_ids=allowed_channel_ids,
        bot_user_id=bot_user_id,
    )

    def _capture_audit(action: str, data: dict[str, Any]) -> None:
        audit_events.append((action, data))

    adapter._audit = _capture_audit  # type: ignore[method-assign]
    return adapter, received, audit_events


def _make_posted_envelope(
    post_id: str = "post-001",
    channel_id: str = "ch-integration",
    user_id: str = "u-alice",
    message: str = "help me with compliance",
) -> dict[str, Any]:
    post = {
        "id": post_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "message": message,
    }
    return {"event": "posted", "data": {"post": json.dumps(post)}}


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_inbound_posted_event_dispatched(
    fake_mm_server: FakeMattermostServer,
) -> None:
    """Fake server pushes a 'posted' WS event -> on_message called with correct fields."""
    adapter, received, _ = _make_adapter_for_server(
        fake_mm_server, allowed_channel_ids=["ch-integration"]
    )
    await adapter.connect()

    # Wait briefly for the WS loop to connect.
    for _ in range(20):
        if adapter._connected:
            break
        await asyncio.sleep(0.05)
    assert adapter._connected, "Adapter did not connect to fake server"

    await fake_mm_server.push_event(
        _make_posted_envelope(channel_id="ch-integration", user_id="u-alice")
    )

    # Give the event loop a moment to dispatch the inbound event.
    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.05)

    await adapter.disconnect()

    assert len(received) == 1
    ev = received[0]
    assert ev.platform == "mattermost"
    assert ev.session_key == "mattermost:ch-integration:u-alice"
    assert ev.message == "help me with compliance"
    assert ev.user_did == "mattermost:u-alice"


async def test_audit_chain_carries_platform_mattermost(
    fake_mm_server: FakeMattermostServer,
) -> None:
    """After send(), adapter emits gateway.message.delivered with platform=mattermost."""
    adapter, _, audit_events = _make_adapter_for_server(fake_mm_server)
    await adapter.connect()

    for _ in range(20):
        if adapter._connected:
            break
        await asyncio.sleep(0.05)

    target = DeliveryTarget.parse("mattermost:ch-audit")
    await adapter.send(target, "audit test message")

    # Give drain task time to POST.
    for _ in range(30):
        delivered = [a for a, _ in audit_events if a == "gateway.message.delivered"]
        if delivered:
            break
        await asyncio.sleep(0.05)

    await adapter.disconnect()

    delivered_events = [
        (a, d) for a, d in audit_events if a == "gateway.message.delivered"
    ]
    assert len(delivered_events) >= 1
    _, data = delivered_events[0]
    assert data.get("platform") == "mattermost"


async def test_send_posts_to_rest_api(
    fake_mm_server: FakeMattermostServer,
) -> None:
    """Adapter.send() -> drain task -> POST /api/v4/posts recorded by fake server."""
    adapter, _, _ = _make_adapter_for_server(fake_mm_server)
    await adapter.connect()

    for _ in range(20):
        if adapter._connected:
            break
        await asyncio.sleep(0.05)

    target = DeliveryTarget.parse("mattermost:ch-rest")
    await adapter.send(target, "rest api test")

    for _ in range(30):
        if fake_mm_server.recorded_posts:
            break
        await asyncio.sleep(0.05)

    await adapter.disconnect()

    assert len(fake_mm_server.recorded_posts) >= 1
    post = fake_mm_server.recorded_posts[0]
    assert post["channel_id"] == "ch-rest"
    assert post["message"] == "rest api test"


async def test_reconnect_on_transient_ws_close(
    fake_mm_server: FakeMattermostServer,
) -> None:
    """After WS close, adapter reconnects and continues to process events."""
    adapter, received, _ = _make_adapter_for_server(
        fake_mm_server, allowed_channel_ids=["ch-reconnect"]
    )
    await adapter.connect()

    for _ in range(20):
        if adapter._connected:
            break
        await asyncio.sleep(0.05)
    assert adapter._connected

    # Force-close the WS connection.
    await fake_mm_server.close_all_connections()

    # Wait for the adapter to notice the disconnect.
    for _ in range(20):
        if not adapter._connected:
            break
        await asyncio.sleep(0.05)

    # Adapter should reconnect (backoff is 800ms); give it 3s total.
    for _ in range(30):
        if adapter._connected:
            break
        await asyncio.sleep(0.1)

    # After reconnect, dispatch another event.
    await fake_mm_server.push_event(
        _make_posted_envelope(
            channel_id="ch-reconnect",
            user_id="u-reconnect",
            post_id="after-reconnect",
        )
    )

    for _ in range(20):
        if received:
            break
        await asyncio.sleep(0.05)

    await adapter.disconnect()

    assert len(received) >= 1


async def test_federal_tier_rejects_non_intranet_url() -> None:
    """Adapter constructor raises ValueError for public host at federal tier.

    This test does NOT start a fake server -- the guard fires before connect().
    We mock getaddrinfo so the test is hermetic (no real DNS lookup).
    """
    with patch(
        "arcgateway.adapters.mattermost.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("203.0.113.1", 0))],
    ):
        with pytest.raises(ValueError, match="federal tier requires an intranet"):
            MattermostAdapter(
                server_url="https://mattermost.public.example.com",
                bot_token=_BOT_TOKEN,
                on_message=lambda e: None,  # type: ignore[arg-type]
                tier="federal",
            )


async def test_disconnect_stops_ws_task(
    fake_mm_server: FakeMattermostServer,
) -> None:
    """After adapter.disconnect(), no further events are dispatched."""
    adapter, received, _ = _make_adapter_for_server(
        fake_mm_server, allowed_channel_ids=["ch-stop"]
    )
    await adapter.connect()

    for _ in range(20):
        if adapter._connected:
            break
        await asyncio.sleep(0.05)

    await adapter.disconnect()
    assert not adapter._connected

    # Push event after disconnect -- should NOT reach on_message.
    count_before = len(received)
    await fake_mm_server.push_event(
        _make_posted_envelope(channel_id="ch-stop", post_id="after-stop")
    )
    await asyncio.sleep(0.1)
    assert len(received) == count_before, (
        "Events must not be dispatched after disconnect()"
    )


async def test_long_message_split_across_multiple_posts(
    fake_mm_server: FakeMattermostServer,
) -> None:
    """A message exceeding 4000 chars is split; each chunk is POSTed separately."""
    adapter, _, _ = _make_adapter_for_server(fake_mm_server)
    await adapter.connect()

    for _ in range(20):
        if adapter._connected:
            break
        await asyncio.sleep(0.05)

    long_message = "A" * 4001
    target = DeliveryTarget.parse("mattermost:ch-long")
    await adapter.send(target, long_message)

    for _ in range(30):
        if len(fake_mm_server.recorded_posts) >= 2:
            break
        await asyncio.sleep(0.05)

    await adapter.disconnect()

    assert len(fake_mm_server.recorded_posts) >= 2
    total_content = "".join(p["message"] for p in fake_mm_server.recorded_posts)
    assert total_content == long_message
