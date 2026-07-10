"""Unit tests for PairingInterceptor — extracted from SessionRouter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from arcgateway.delivery import DeliveryTarget
from arcgateway.executor import InboundEvent
from arcgateway.session import build_session_key
from arcgateway.session_pairing import PairingInterceptor


def _make_event(
    user_did: str = "did:arc:telegram:user1",
    platform: str = "telegram",
    chat_id: str = "chat_123",
) -> InboundEvent:
    return InboundEvent(
        platform=platform,
        chat_id=chat_id,
        user_did=user_did,
        agent_did="did:arc:agent:bot",
        session_key=build_session_key("did:arc:agent:bot", user_did),
        message="hello",
    )


# ---------------------------------------------------------------------------
# Allowlist management
# ---------------------------------------------------------------------------


class TestPairingInterceptorAllowlist:
    @pytest.mark.asyncio
    async def test_no_allowlist_no_store_approves_everyone(self) -> None:
        """When user_allowlist and pairing_store are both None, all users are approved.

        This is the enforcement-disabled default (require_pairing=false).
        """
        interceptor = PairingInterceptor(user_allowlist=None)
        assert await interceptor.is_user_approved("did:arc:user:anyone", "telegram") is True

    @pytest.mark.asyncio
    async def test_empty_allowlist_no_store_rejects_unknown(self) -> None:
        """Empty set allowlist, no store, rejects all users."""
        interceptor = PairingInterceptor(user_allowlist=set())
        assert await interceptor.is_user_approved("did:arc:user:alice", "telegram") is False

    @pytest.mark.asyncio
    async def test_add_approved_user_enables_approval(self) -> None:
        """After add_approved_user, the DID passes is_user_approved."""
        interceptor = PairingInterceptor(user_allowlist=set())
        interceptor.add_approved_user("did:arc:user:alice")
        assert await interceptor.is_user_approved("did:arc:user:alice", "telegram") is True

    @pytest.mark.asyncio
    async def test_add_approved_user_auto_creates_set(self) -> None:
        """add_approved_user with None allowlist creates a new set."""
        interceptor = PairingInterceptor(user_allowlist=None)
        interceptor.add_approved_user("did:arc:user:bob")
        # Now enforcement is active — only bob is approved.
        assert await interceptor.is_user_approved("did:arc:user:bob", "telegram") is True
        assert await interceptor.is_user_approved("did:arc:user:alice", "telegram") is False

    @pytest.mark.asyncio
    async def test_remove_approved_user(self) -> None:
        """remove_approved_user removes the DID from the allowlist."""
        interceptor = PairingInterceptor(user_allowlist={"did:arc:user:alice", "did:arc:user:bob"})
        interceptor.remove_approved_user("did:arc:user:alice")
        assert await interceptor.is_user_approved("did:arc:user:alice", "telegram") is False
        assert await interceptor.is_user_approved("did:arc:user:bob", "telegram") is True

    @pytest.mark.asyncio
    async def test_remove_approved_user_noop_on_none_allowlist(self) -> None:
        """remove_approved_user is a no-op when allowlist is None."""
        interceptor = PairingInterceptor(user_allowlist=None)
        # Should not raise
        interceptor.remove_approved_user("did:arc:user:ghost")
        assert await interceptor.is_user_approved("did:arc:user:ghost", "telegram") is True


class TestPairingInterceptorStoreBackedApproval:
    """is_user_approved must consult the PairingStore live, not a frozen list."""

    @pytest.mark.asyncio
    async def test_store_present_no_allowlist_denies_unapproved(self) -> None:
        """Wiring a pairing_store (no static allowlist) activates enforcement."""
        mock_store = MagicMock()
        mock_store.is_approved = AsyncMock(return_value=False)

        interceptor = PairingInterceptor(pairing_store=mock_store)
        result = await interceptor.is_user_approved("did:arc:telegram:99", "telegram")

        assert result is False
        mock_store.is_approved.assert_awaited_once_with("telegram", "did:arc:telegram:99")

    @pytest.mark.asyncio
    async def test_store_present_no_allowlist_allows_approved(self) -> None:
        """A user the store reports as approved passes even with no static allowlist.

        This is the cross-process contract: an `arc gateway pair approve` from a
        separate CLI process writes to the store's SQLite db; the live gateway's
        interceptor must see it on the very next check.
        """
        mock_store = MagicMock()
        mock_store.is_approved = AsyncMock(return_value=True)

        interceptor = PairingInterceptor(pairing_store=mock_store)
        result = await interceptor.is_user_approved("did:arc:telegram:99", "telegram")

        assert result is True

    @pytest.mark.asyncio
    async def test_static_allowlist_short_circuits_store_check(self) -> None:
        """A user already in the static allowlist is approved without a store lookup."""
        mock_store = MagicMock()
        mock_store.is_approved = AsyncMock(return_value=False)

        interceptor = PairingInterceptor(
            user_allowlist={"did:arc:user:alice"},
            pairing_store=mock_store,
        )
        result = await interceptor.is_user_approved("did:arc:user:alice", "telegram")

        assert result is True
        mock_store.is_approved.assert_not_awaited()


# ---------------------------------------------------------------------------
# handle_unpaired_user — no pairing store
# ---------------------------------------------------------------------------


class TestHandleUnpairedUserNoStore:
    @pytest.mark.asyncio
    async def test_no_store_returns_without_error(self) -> None:
        """Without a pairing store, handle_unpaired_user returns silently."""
        interceptor = PairingInterceptor(user_allowlist=set())
        event = _make_event()
        # Should not raise
        await interceptor.handle_unpaired_user(event)


# ---------------------------------------------------------------------------
# handle_unpaired_user — with pairing store and adapter_map
# ---------------------------------------------------------------------------


class TestHandleUnpairedUserWithStore:
    @pytest.mark.asyncio
    async def test_mint_code_delivers_dm_via_adapter(self) -> None:
        """When an unapproved user messages, a pairing code is minted and DM'd."""
        from arcgateway.pairing import PairingCode

        mock_store = MagicMock()
        mock_store.mint_code = AsyncMock(
            return_value=PairingCode(
                code="ABCD1234",
                platform="telegram",
                platform_user_id_hash="hash123",
                minted_at=1000.0,
                expires_at=4600.0,
            )
        )
        mock_adapter = AsyncMock()
        mock_adapter.send = AsyncMock()

        interceptor = PairingInterceptor(
            user_allowlist=set(),
            pairing_store=mock_store,
            adapter_map={"telegram": mock_adapter},
        )
        event = _make_event()
        await interceptor.handle_unpaired_user(event)

        mock_store.mint_code.assert_called_once()
        mock_adapter.send.assert_called_once()
        call_args = mock_adapter.send.call_args
        sent_target = call_args[0][0]
        sent_message = call_args[0][1]
        # adapter.send()'s real Protocol (BasePlatformAdapter.send) takes a
        # DeliveryTarget, not a raw chat_id string — a real adapter (e.g.
        # TelegramAdapter) does `target.chat_id` internally and raises
        # AttributeError on a plain str. Every mock-based test before this
        # one missed that shape mismatch entirely.
        assert isinstance(sent_target, DeliveryTarget)
        assert sent_target.platform == "telegram"
        assert sent_target.chat_id == "chat_123"
        assert "ABCD1234" in sent_message
        assert "arc gateway pair approve ABCD1234" in sent_message

    @pytest.mark.asyncio
    async def test_rate_limited_sends_reminder(self) -> None:
        """PairingRateLimited triggers a reminder DM."""
        from arcgateway.pairing import PairingRateLimited

        mock_store = MagicMock()
        mock_store.mint_code = AsyncMock(side_effect=PairingRateLimited("rate limit"))
        mock_adapter = AsyncMock()

        interceptor = PairingInterceptor(
            user_allowlist=set(),
            pairing_store=mock_store,
            adapter_map={"telegram": mock_adapter},
        )
        event = _make_event()
        await interceptor.handle_unpaired_user(event)

        mock_adapter.send.assert_called_once()
        assert "pending pairing code" in mock_adapter.send.call_args[0][1].lower()

    @pytest.mark.asyncio
    async def test_platform_full_sends_notification(self) -> None:
        """PairingPlatformFull triggers a notification DM."""
        from arcgateway.pairing import PairingPlatformFull

        mock_store = MagicMock()
        mock_store.mint_code = AsyncMock(side_effect=PairingPlatformFull("full"))
        mock_adapter = AsyncMock()

        interceptor = PairingInterceptor(
            user_allowlist=set(),
            pairing_store=mock_store,
            adapter_map={"telegram": mock_adapter},
        )
        event = _make_event()
        await interceptor.handle_unpaired_user(event)

        mock_adapter.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_platform_locked_sends_notification(self) -> None:
        """PairingPlatformLocked triggers a notification DM."""
        from arcgateway.pairing import PairingPlatformLocked

        mock_store = MagicMock()
        mock_store.mint_code = AsyncMock(side_effect=PairingPlatformLocked("locked"))
        mock_adapter = AsyncMock()

        interceptor = PairingInterceptor(
            user_allowlist=set(),
            pairing_store=mock_store,
            adapter_map={"telegram": mock_adapter},
        )
        event = _make_event()
        await interceptor.handle_unpaired_user(event)

        mock_adapter.send.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_adapter_for_platform_skips_dm(self) -> None:
        """When no adapter is registered for the platform, DM is silently skipped."""
        from arcgateway.pairing import PairingCode

        mock_store = MagicMock()
        mock_store.mint_code = AsyncMock(
            return_value=PairingCode(
                code="WXYZ9876",
                platform="slack",
                platform_user_id_hash="hash456",
                minted_at=1000.0,
                expires_at=4600.0,
            )
        )

        interceptor = PairingInterceptor(
            user_allowlist=set(),
            pairing_store=mock_store,
            adapter_map={},  # No slack adapter registered
        )
        event = _make_event(platform="slack")
        # Should not raise
        await interceptor.handle_unpaired_user(event)

    @pytest.mark.asyncio
    async def test_register_adapter_after_construction(self) -> None:
        """Adapters can be registered after interceptor construction."""
        from arcgateway.pairing import PairingCode

        mock_store = MagicMock()
        mock_store.mint_code = AsyncMock(
            return_value=PairingCode(
                code="LATE5678",
                platform="telegram",
                platform_user_id_hash="lhash",
                minted_at=1000.0,
                expires_at=4600.0,
            )
        )
        mock_adapter = AsyncMock()

        interceptor = PairingInterceptor(
            user_allowlist=set(),
            pairing_store=mock_store,
        )
        interceptor.register_adapter("telegram", mock_adapter)

        event = _make_event()
        await interceptor.handle_unpaired_user(event)
        mock_adapter.send.assert_called_once()
