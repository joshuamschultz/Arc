"""Unit tests for PairingInterceptor — extracted from SessionRouter."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

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
    def test_no_allowlist_approves_everyone(self) -> None:
        """When user_allowlist is None, all users are approved."""
        interceptor = PairingInterceptor(user_allowlist=None)
        assert interceptor.is_user_approved("did:arc:user:anyone") is True

    def test_empty_allowlist_rejects_unknown(self) -> None:
        """Empty set allowlist rejects all users."""
        interceptor = PairingInterceptor(user_allowlist=set())
        assert interceptor.is_user_approved("did:arc:user:alice") is False

    def test_add_approved_user_enables_approval(self) -> None:
        """After add_approved_user, the DID passes is_user_approved."""
        interceptor = PairingInterceptor(user_allowlist=set())
        interceptor.add_approved_user("did:arc:user:alice")
        assert interceptor.is_user_approved("did:arc:user:alice") is True

    def test_add_approved_user_auto_creates_set(self) -> None:
        """add_approved_user with None allowlist creates a new set."""
        interceptor = PairingInterceptor(user_allowlist=None)
        interceptor.add_approved_user("did:arc:user:bob")
        # Now enforcement is active — only bob is approved.
        assert interceptor.is_user_approved("did:arc:user:bob") is True
        assert interceptor.is_user_approved("did:arc:user:alice") is False

    def test_remove_approved_user(self) -> None:
        """remove_approved_user removes the DID from the allowlist."""
        interceptor = PairingInterceptor(user_allowlist={"did:arc:user:alice", "did:arc:user:bob"})
        interceptor.remove_approved_user("did:arc:user:alice")
        assert interceptor.is_user_approved("did:arc:user:alice") is False
        assert interceptor.is_user_approved("did:arc:user:bob") is True

    def test_remove_approved_user_noop_on_none_allowlist(self) -> None:
        """remove_approved_user is a no-op when allowlist is None."""
        interceptor = PairingInterceptor(user_allowlist=None)
        # Should not raise
        interceptor.remove_approved_user("did:arc:user:ghost")
        assert interceptor.is_user_approved("did:arc:user:ghost") is True


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
        sent_message = call_args[0][1]
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
