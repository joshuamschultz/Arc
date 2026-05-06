"""Unit tests for MattermostAdapter.

Contract tests verify every method in BasePlatformAdapter is implemented.
Behavioral tests cover:
  - Construction and audit emission
  - Federal-tier air-gap guard (public host raises ValueError)
  - Token-no-leak (repr/str/dir safety)
  - Session key follows Slack pattern (mattermost:{channel}:{user})
  - Per-channel outbound queue with drop-oldest backpressure
  - send() while not connected -> gateway.message.dropped
  - Inbound event dispatching from _handle_ws_message
  - Deduplication on post_id
  - split_message boundary-aware chunking
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch

import pytest

from arcgateway.adapters.base import BasePlatformAdapter
from arcgateway.adapters.mattermost import MattermostAdapter, _split_message
from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import InboundEvent

# NOTE: avoid a module-level `pytestmark = pytest.mark.asyncio`. The file
# mixes sync (contract checks, federal-tier guard validation, repr safety,
# split helper) and async tests — a module-level mark adds an asyncio mark
# to every sync test and pytest emits a warning per test. We decorate the
# async test classes individually instead.

# Canary values -- any appearance in repr/str/logs is a test failure.
_PAT_CANARY = "mm-pat-canary-leak-99999999999999"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_adapter(
    *,
    server_url: str = "http://localhost:8065",
    allowed_channel_ids: list[str] | None = None,
    bot_user_id: str = "bot-uid",
    tier: str = "personal",
    intranet_domains: list[str] | None = None,
) -> tuple[MattermostAdapter, list[InboundEvent], list[tuple[str, dict[str, Any]]]]:
    received: list[InboundEvent] = []
    audit_events: list[tuple[str, dict[str, Any]]] = []

    async def _on_message(event: InboundEvent) -> None:
        received.append(event)

    adapter = MattermostAdapter(
        server_url=server_url,
        bot_token=_PAT_CANARY,
        on_message=_on_message,
        allowed_channel_ids=allowed_channel_ids,
        bot_user_id=bot_user_id,
        tier=tier,
        intranet_domains=intranet_domains,
    )
    # Replace audit emitter to capture events without a real sink.

    def _capture_audit(action: str, data: dict[str, Any]) -> None:
        audit_events.append((action, data))

    adapter._audit = _capture_audit  # type: ignore[method-assign]
    return adapter, received, audit_events


def _make_posted_envelope(
    post_id: str = "post-001",
    channel_id: str = "ch-abc",
    user_id: str = "u-alice",
    message: str = "hello agent",
) -> str:
    post = {
        "id": post_id,
        "channel_id": channel_id,
        "user_id": user_id,
        "message": message,
    }
    return json.dumps({"event": "posted", "data": {"post": json.dumps(post)}})


# ---------------------------------------------------------------------------
# Contract: MattermostAdapter satisfies BasePlatformAdapter Protocol
# ---------------------------------------------------------------------------


class TestBasePlatformAdapterContract:
    def test_implements_base_platform_adapter_protocol(self) -> None:
        """isinstance check against the runtime-checkable Protocol."""
        adapter, _, _ = _make_adapter()
        assert isinstance(adapter, BasePlatformAdapter), (
            "MattermostAdapter must satisfy BasePlatformAdapter Protocol"
        )

    def test_has_name_attribute(self) -> None:
        adapter, _, _ = _make_adapter()
        assert adapter.name == "mattermost"

    def test_has_connect_method(self) -> None:
        adapter, _, _ = _make_adapter()
        assert callable(getattr(adapter, "connect", None))

    def test_has_disconnect_method(self) -> None:
        adapter, _, _ = _make_adapter()
        assert callable(getattr(adapter, "disconnect", None))

    def test_has_send_method(self) -> None:
        adapter, _, _ = _make_adapter()
        assert callable(getattr(adapter, "send", None))

    def test_has_send_with_id_method(self) -> None:
        adapter, _, _ = _make_adapter()
        assert callable(getattr(adapter, "send_with_id", None))


# ---------------------------------------------------------------------------
# Token no-leak (mirroring test_slack_token_no_leak.py)
# ---------------------------------------------------------------------------


class TestTokenNoLeak:
    def test_repr_does_not_include_token(self) -> None:
        adapter, _, _ = _make_adapter()
        rendered = repr(adapter)
        assert _PAT_CANARY not in rendered, (
            "MattermostAdapter repr leaked the PAT -- secrets must never appear in repr"
        )

    def test_str_does_not_include_token(self) -> None:
        adapter, _, _ = _make_adapter()
        rendered = str(adapter)
        assert _PAT_CANARY not in rendered

    def test_dir_does_not_expose_token_attr(self) -> None:
        """No public attribute named 'bot_token' to prevent accidental repr leakage."""
        adapter, _, _ = _make_adapter()
        public = {a for a in dir(adapter) if not a.startswith("_")}
        assert "bot_token" not in public, (
            "Public 'bot_token' attribute would leak via repr / serializers"
        )

    def test_repr_shows_server_url_and_tier(self) -> None:
        adapter, _, _ = _make_adapter(server_url="http://192.168.1.10:8065", tier="federal")
        rendered = repr(adapter)
        assert "192.168.1.10" in rendered
        assert "federal" in rendered


# ---------------------------------------------------------------------------
# Federal-tier air-gap guard
# ---------------------------------------------------------------------------


class TestFederalAirgapGuard:
    def test_loopback_accepted_at_federal_tier(self) -> None:
        """localhost / 127.x.x.x are always valid for federal tier."""
        adapter, _, _ = _make_adapter(
            server_url="http://localhost:8065", tier="federal"
        )
        assert adapter is not None

    def test_rfc1918_10_accepted_at_federal_tier(self) -> None:
        adapter, _, _ = _make_adapter(
            server_url="http://10.0.0.5:8065", tier="federal"
        )
        assert adapter is not None

    def test_rfc1918_172_accepted_at_federal_tier(self) -> None:
        adapter, _, _ = _make_adapter(
            server_url="http://172.16.42.1:8065", tier="federal"
        )
        assert adapter is not None

    def test_rfc1918_192_168_accepted_at_federal_tier(self) -> None:
        adapter, _, _ = _make_adapter(
            server_url="http://192.168.0.100:8065", tier="federal"
        )
        assert adapter is not None

    def test_intranet_domain_accepted_at_federal_tier(self) -> None:
        adapter, _, _ = _make_adapter(
            server_url="https://mattermost.internal.doe.gov",
            tier="federal",
            intranet_domains=["mattermost.internal.doe.gov"],
        )
        assert adapter is not None

    def test_public_host_rejected_at_federal_tier(self) -> None:
        """A public hostname must raise ValueError at federal tier.

        This is the air-gap contract: zero outbound DNS calls to public
        hosts (SPEC-025 §NFR-5).  The guard inspects the resolved IP
        addresses; we mock getaddrinfo to return a routable public IP.
        """
        with patch(
            "arcgateway.adapters.mattermost.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("1.2.3.4", 0))],
        ):
            with pytest.raises(ValueError, match="federal tier requires an intranet"):
                MattermostAdapter(
                    server_url="https://mattermost.example.com",
                    bot_token=_PAT_CANARY,
                    on_message=lambda e: None,  # type: ignore[arg-type]
                    tier="federal",
                )

    def test_public_host_accepted_at_personal_tier(self) -> None:
        """Personal tier does not enforce the air-gap guard."""
        adapter, _, _ = _make_adapter(
            server_url="https://mattermost.example.com",
            tier="personal",
        )
        assert adapter is not None

    def test_airgap_rejects_on_dns_failure(self) -> None:
        """SPEC-025 §H-1 — DNS resolution failure is fail-CLOSED.

        Previously the guard returned True on ``gaierror`` (treat as
        intranet). An attacker who can blackhole DNS would slip past the
        check. Air-gap now requires the operator to add the host to
        ``intranet_domains`` explicitly when DNS is unreachable.
        """
        import socket as _socket

        with patch(
            "arcgateway.adapters.mattermost.socket.getaddrinfo",
            side_effect=_socket.gaierror("temporary failure"),
        ):
            with pytest.raises(ValueError, match="federal tier requires"):
                MattermostAdapter(
                    server_url="https://mm.unreachable.local",
                    bot_token=_PAT_CANARY,
                    on_message=lambda e: None,  # type: ignore[arg-type]
                    tier="federal",
                )

    def test_airgap_explicit_intranet_domain_overrides_dns_failure(self) -> None:
        """A hostname listed in ``intranet_domains`` is accepted even if DNS fails.

        This is the explicit-override path the operator uses when DNS is
        unreachable but they know the hostname IS intranet (the very
        reason for keeping ``intranet_domains`` after the H-1 fix).
        """
        import socket as _socket

        with patch(
            "arcgateway.adapters.mattermost.socket.getaddrinfo",
            side_effect=_socket.gaierror("temporary failure"),
        ):
            adapter, _, _ = _make_adapter(
                server_url="https://mm.unreachable.local",
                tier="federal",
                intranet_domains=["mm.unreachable.local"],
            )
            assert adapter is not None

    def test_intranet_domains_entry_resolving_to_public_rejected(self) -> None:
        """SPEC-025 §H-2 — allow-list entries that resolve public are rejected.

        Without this, ``intranet_domains=["example.com"]`` silently
        bypasses the entire guard. Construction must fail loud.
        """
        with patch(
            "arcgateway.adapters.mattermost.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("93.184.216.34", 0))],
        ):
            with pytest.raises(ValueError, match="resolves to a public address"):
                MattermostAdapter(
                    server_url="https://10.0.0.5",
                    bot_token=_PAT_CANARY,
                    on_message=lambda e: None,  # type: ignore[arg-type]
                    tier="federal",
                    intranet_domains=["example.com"],
                )

    def test_intranet_domains_unresolvable_entry_accepted(self) -> None:
        """A resolvable-only-on-target-network host is accepted in the allow-list.

        ``intranet_domains`` is the operator's explicit override for
        hostnames that cannot resolve from the validation host (e.g., the
        operator's laptop runs the deploy but the agent runs on a SCIF
        network where the hostname resolves). Trust the operator on
        unresolvable entries.
        """
        import socket as _socket

        with patch(
            "arcgateway.adapters.mattermost.socket.getaddrinfo",
            side_effect=_socket.gaierror("temporary failure"),
        ):
            # First call resolves the entry, second call resolves server_url.
            # Both raise gaierror, both must be tolerated under the H-2 fix.
            adapter, _, _ = _make_adapter(
                server_url="https://mm.scif.local",
                tier="federal",
                intranet_domains=["mm.scif.local"],
            )
            assert adapter is not None

    def test_intranet_domains_exact_match_no_suffix_collision(self) -> None:
        """``intranet_domains=["intranet.local"]`` does NOT match
        ``attacker.com.intranet.local`` — exact-hostname match only.

        SPEC-025 §H-2 — an attacker who controls a public DNS that
        happens to end with an intranet suffix must not slip past the
        guard. Documented as exact match.
        """
        def _resolve(host: str, *_args: Any, **_kw: Any) -> list[tuple[Any, Any, Any, Any, tuple[str, int]]]:
            # Allow-list entry resolves intranet (private 10/8), so the
            # entry-validation step passes. The attacker's hostname
            # resolves to a public IP, so the server_url step rejects.
            if host == "intranet.local":
                return [(None, None, None, None, ("10.0.0.1", 0))]
            return [(None, None, None, None, ("93.184.216.34", 0))]

        with patch(
            "arcgateway.adapters.mattermost.socket.getaddrinfo",
            side_effect=_resolve,
        ):
            with pytest.raises(ValueError, match="federal tier requires"):
                MattermostAdapter(
                    server_url="https://attacker.com.intranet.local",
                    bot_token=_PAT_CANARY,
                    on_message=lambda e: None,  # type: ignore[arg-type]
                    tier="federal",
                    intranet_domains=["intranet.local"],
                )

    def test_error_message_does_not_contain_token(self) -> None:
        """The ValueError message must not echo the PAT."""
        with patch(
            "arcgateway.adapters.mattermost.socket.getaddrinfo",
            return_value=[(None, None, None, None, ("8.8.8.8", 0))],
        ):
            with pytest.raises(ValueError) as exc_info:
                MattermostAdapter(
                    server_url="https://external.example.com",
                    bot_token=_PAT_CANARY,
                    on_message=lambda e: None,  # type: ignore[arg-type]
                    tier="federal",
                )
            assert _PAT_CANARY not in str(exc_info.value)


# ---------------------------------------------------------------------------
# Session key contract (ADR-002)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSessionKey:
    async def test_session_key_follows_mattermost_pattern(self) -> None:
        """Session key must be mattermost:{channel_id}:{user_id} per ADR-002."""
        adapter, received, _ = _make_adapter(allowed_channel_ids=["ch-abc"])
        envelope = _make_posted_envelope(
            channel_id="ch-abc", user_id="u-alice", post_id="p-1"
        )
        await adapter._handle_ws_message(envelope)
        assert len(received) == 1
        assert received[0].session_key == "mattermost:ch-abc:u-alice"

    async def test_user_did_includes_platform_prefix(self) -> None:
        adapter, received, _ = _make_adapter(allowed_channel_ids=["ch-abc"])
        envelope = _make_posted_envelope(
            channel_id="ch-abc", user_id="u-bob", post_id="p-2"
        )
        await adapter._handle_ws_message(envelope)
        assert received[0].user_did == "mattermost:u-bob"

    async def test_platform_field_is_mattermost(self) -> None:
        adapter, received, _ = _make_adapter(allowed_channel_ids=["ch-x"])
        envelope = _make_posted_envelope(channel_id="ch-x", post_id="p-3")
        await adapter._handle_ws_message(envelope)
        assert received[0].platform == "mattermost"


# ---------------------------------------------------------------------------
# Inbound event dispatching
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestInboundDispatching:
    async def test_posted_event_dispatched(self) -> None:
        adapter, received, _ = _make_adapter(allowed_channel_ids=["ch-1"])
        await adapter._handle_ws_message(
            _make_posted_envelope(channel_id="ch-1", post_id="p-10")
        )
        assert len(received) == 1
        assert received[0].message == "hello agent"

    async def test_non_posted_event_ignored(self) -> None:
        adapter, received, _ = _make_adapter()
        await adapter._handle_ws_message(
            json.dumps({"event": "reaction_added", "data": {}})
        )
        assert len(received) == 0

    async def test_own_post_skipped(self) -> None:
        adapter, received, _ = _make_adapter(bot_user_id="bot-uid")
        envelope = _make_posted_envelope(user_id="bot-uid", post_id="p-own")
        await adapter._handle_ws_message(envelope)
        assert len(received) == 0

    async def test_disallowed_channel_rejected(self) -> None:
        adapter, received, _ = _make_adapter(allowed_channel_ids=["ch-allowed"])
        envelope = _make_posted_envelope(channel_id="ch-other", post_id="p-other")
        await adapter._handle_ws_message(envelope)
        assert len(received) == 0

    async def test_all_channels_accepted_when_list_empty(self) -> None:
        """Empty allowed_channel_ids means accept all channels."""
        adapter, received, _ = _make_adapter(allowed_channel_ids=[])
        envelope = _make_posted_envelope(channel_id="any-channel", post_id="p-any")
        await adapter._handle_ws_message(envelope)
        assert len(received) == 1

    async def test_invalid_json_ignored(self) -> None:
        adapter, received, _ = _make_adapter()
        await adapter._handle_ws_message("NOT VALID JSON{{{")
        assert len(received) == 0

    async def test_empty_message_not_dispatched(self) -> None:
        adapter, received, _ = _make_adapter(allowed_channel_ids=["ch-1"])
        envelope = _make_posted_envelope(channel_id="ch-1", message="", post_id="p-empty")
        await adapter._handle_ws_message(envelope)
        assert len(received) == 0


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDeduplication:
    async def test_same_post_id_dispatched_once(self) -> None:
        adapter, received, _ = _make_adapter(allowed_channel_ids=["ch-1"])
        envelope = _make_posted_envelope(channel_id="ch-1", post_id="dedup-01")
        await adapter._handle_ws_message(envelope)
        await adapter._handle_ws_message(envelope)
        assert len(received) == 1

    async def test_different_post_ids_both_dispatched(self) -> None:
        adapter, received, _ = _make_adapter(allowed_channel_ids=["ch-1"])
        await adapter._handle_ws_message(
            _make_posted_envelope(channel_id="ch-1", post_id="a-01", message="first")
        )
        await adapter._handle_ws_message(
            _make_posted_envelope(channel_id="ch-1", post_id="a-02", message="second")
        )
        assert len(received) == 2


# ---------------------------------------------------------------------------
# send() while disconnected
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestSendWhileDisconnected:
    async def test_send_while_not_connected_emits_dropped_audit(self) -> None:
        adapter, _, audit_events = _make_adapter()
        target = DeliveryTarget.parse("mattermost:ch-1")
        await adapter.send(target, "hello")
        dropped = [a for a, _ in audit_events if a == "gateway.message.dropped"]
        assert len(dropped) >= 1

    async def test_send_with_id_returns_none(self) -> None:
        adapter, _, _ = _make_adapter()
        target = DeliveryTarget.parse("mattermost:ch-1")
        result = await adapter.send_with_id(target, "hi")
        assert result is None


# ---------------------------------------------------------------------------
# Outbound queue: drop-oldest backpressure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestOutboundQueueBackpressure:
    async def test_queue_drop_oldest_when_full(self) -> None:
        """When the channel queue is full, oldest frame is dropped."""
        adapter, _, audit_events = _make_adapter()
        adapter._connected = True

        ch = "ch-full"
        # Use a tiny queue (maxsize=2) without a drain task so we can test the
        # drop-oldest path directly without needing aiohttp.
        q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=2)
        adapter._outbound_queues[ch] = q

        # Fill to capacity.
        adapter._enqueue(ch, q, "msg-1")
        adapter._enqueue(ch, q, "msg-2")
        # This call should drop msg-1 and enqueue msg-3.
        adapter._enqueue(ch, q, "msg-3")

        # Queue still has 2 items (msg-2 and msg-3).
        assert q.qsize() == 2
        dropped = [d for _, d in audit_events if d.get("reason") == "queue_full_drop_oldest"]
        assert len(dropped) == 1


# ---------------------------------------------------------------------------
# Disconnect lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
class TestDisconnectLifecycle:
    async def test_disconnect_emits_adapter_disconnect_audit(self) -> None:
        adapter, _, audit_events = _make_adapter()
        await adapter.disconnect()
        disconnect_events = [a for a, _ in audit_events if a == "gateway.adapter.disconnect"]
        assert len(disconnect_events) >= 1

    async def test_disconnect_is_idempotent(self) -> None:
        """Calling disconnect twice must not raise."""
        adapter, _, _ = _make_adapter()
        await adapter.disconnect()
        await adapter.disconnect()  # must not raise

    async def test_disconnect_cancels_ws_task(self) -> None:
        adapter, _, _ = _make_adapter()
        # Simulate a running WS task.
        mock_task = asyncio.create_task(asyncio.sleep(9999))
        adapter._ws_task = mock_task
        await adapter.disconnect()
        assert mock_task.cancelled() or mock_task.done()


# ---------------------------------------------------------------------------
# Construction audit event
# ---------------------------------------------------------------------------


class TestConstructionAudit:
    """Construction is sync — no asyncio mark needed."""

    def test_construction_emits_register_audit(self) -> None:
        async def _noop(e: InboundEvent) -> None:
            pass

        with patch(
            "arcgateway.adapters.mattermost.MattermostAdapter._audit"
        ) as mock_audit:
            MattermostAdapter(
                server_url="http://localhost:8065",
                bot_token=_PAT_CANARY,
                on_message=_noop,
            )
            mock_audit.assert_called_once()
            call_action = mock_audit.call_args[0][0]
            assert call_action == "gateway.adapter.register"
            call_data = mock_audit.call_args[0][1]
            assert call_data["platform"] == "mattermost"


# ---------------------------------------------------------------------------
# split_message helper
# ---------------------------------------------------------------------------


class TestSplitMessage:
    def test_short_message_not_split(self) -> None:
        chunks = _split_message("hello", max_length=100)
        assert chunks == ["hello"]

    def test_empty_message_returns_empty(self) -> None:
        assert _split_message("") == []

    def test_long_message_splits_at_paragraph_boundary(self) -> None:
        text = "A" * 50 + "\n\n" + "B" * 50
        chunks = _split_message(text, max_length=60)
        assert len(chunks) == 2
        assert chunks[0] == "A" * 50
        assert chunks[1] == "B" * 50

    def test_long_message_splits_at_newline(self) -> None:
        text = "A" * 50 + "\n" + "B" * 50
        chunks = _split_message(text, max_length=60)
        assert len(chunks) == 2

    def test_very_long_word_split_at_max(self) -> None:
        text = "X" * 200
        chunks = _split_message(text, max_length=100)
        assert all(len(c) <= 100 for c in chunks)
        assert "".join(chunks) == text

    def test_exact_max_length_not_split(self) -> None:
        text = "A" * 100
        chunks = _split_message(text, max_length=100)
        assert chunks == [text]
