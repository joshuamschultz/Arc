"""Integration test — unpaired user DMs trigger adapter.send with pairing code.

Verifies that the full SessionRouter + PairingInterceptor + PairingStore chain:
1. Intercepts an unapproved user's message.
2. Mints a pairing code via PairingStore.
3. Delivers the code via adapter.send(), including the operator instruction.

This closes 5 TODO(M1 T1.7 integration) comments from the original session.py.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcgateway.executor import Delta, InboundEvent
from arcgateway.pairing import PairingStore
from arcgateway.session import SessionRouter, build_session_key
from arcgateway.session_pairing import PairingInterceptor


def _make_event(
    user_did: str = "did:arc:telegram:unpaired_user",
    platform: str = "telegram",
    chat_id: str = "chat_dm_99",
) -> InboundEvent:
    return InboundEvent(
        platform=platform,
        chat_id=chat_id,
        user_did=user_did,
        agent_did="did:arc:agent:bot",
        session_key=build_session_key("did:arc:agent:bot", user_did),
        message="Hello agent",
    )


class _NeverCalledExecutor:
    """Executor that fails the test if it is ever called.

    Verifies the pairing interceptor truly stops routing to the agent.
    """

    async def run(self, event: InboundEvent):  # type: ignore[return]
        pytest.fail(
            "Executor.run should NOT be called for an unapproved user "
            f"(user_did={event.user_did!r})"
        )


# ---------------------------------------------------------------------------
# Full-stack: SessionRouter + PairingStore + adapter.send
# ---------------------------------------------------------------------------


class TestPairingDmDelivery:
    @pytest.mark.asyncio
    async def test_unpaired_user_receives_pairing_code_dm(self) -> None:
        """End-to-end: unapproved user DM → mint code → adapter.send called."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pairing.db"
            store = PairingStore(db_path=db_path, tier="personal")

            mock_adapter = MagicMock()
            mock_adapter.send = AsyncMock()

            interceptor = PairingInterceptor(
                user_allowlist=set(),  # no users approved
                pairing_store=store,
                adapter_map={"telegram": mock_adapter},
            )

            # Build a SessionRouter with the interceptor wired
            router = SessionRouter(
                executor=_NeverCalledExecutor(),  # type: ignore[arg-type]
                user_allowlist=set(),
                pairing_store=store,
                adapter_map={"telegram": mock_adapter},
            )

            event = _make_event()
            await router.handle(event)

            # adapter.send must have been called exactly once
            mock_adapter.send.assert_called_once()
            sent_message: str = mock_adapter.send.call_args[0][1]

            # Message must contain the operator instruction
            assert "arc gateway pair approve" in sent_message
            # Message must NOT contain the raw user DID (privacy requirement)
            assert event.user_did not in sent_message

    @pytest.mark.asyncio
    async def test_approved_user_routes_to_agent(self) -> None:
        """After approval, the user's next message routes to the agent executor."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pairing2.db"
            store = PairingStore(db_path=db_path, tier="personal")

            user_did = "did:arc:telegram:approved_alice"
            approved_users = {user_did}

            async def _fast_stream(event: InboundEvent):
                yield Delta(kind="done", content="", is_final=True, turn_id=event.session_key)

            class _FastExecutor:
                call_count = 0

                async def run(self, event: InboundEvent):
                    _FastExecutor.call_count += 1
                    return _fast_stream(event)

            router = SessionRouter(
                executor=_FastExecutor(),  # type: ignore[arg-type]
                user_allowlist=approved_users,
                pairing_store=store,
            )

            event = _make_event(user_did=user_did)
            await router.handle(event)
            await asyncio.sleep(0.05)

            assert _FastExecutor.call_count == 1

    @pytest.mark.asyncio
    async def test_adapter_send_contains_code_and_instruction(self) -> None:
        """The DM message must include the pairing code and the operator command."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pairing3.db"
            store = PairingStore(db_path=db_path, tier="personal")

            mock_adapter = MagicMock()
            mock_adapter.send = AsyncMock()

            router = SessionRouter(
                executor=_NeverCalledExecutor(),  # type: ignore[arg-type]
                user_allowlist=set(),
                pairing_store=store,
                adapter_map={"telegram": mock_adapter},
            )

            await router.handle(_make_event())

            mock_adapter.send.assert_called_once()
            msg: str = mock_adapter.send.call_args[0][1]
            assert "arc gateway pair approve" in msg
            # The code itself should be 8 chars from PAIRING_ALPHABET
            assert any(
                word.isalnum() and len(word) == 8
                for word in msg.split()
            )

    @pytest.mark.asyncio
    async def test_rate_limited_user_receives_reminder(self) -> None:
        """Second message within rate-limit window delivers a reminder DM."""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = Path(tmpdir) / "pairing4.db"
            store = PairingStore(db_path=db_path, tier="personal")

            mock_adapter = MagicMock()
            mock_adapter.send = AsyncMock()

            router = SessionRouter(
                executor=_NeverCalledExecutor(),  # type: ignore[arg-type]
                user_allowlist=set(),
                pairing_store=store,
                adapter_map={"telegram": mock_adapter},
            )

            event = _make_event()
            # First message → mint code
            await router.handle(event)
            # Second message within 10 min → rate-limit reminder
            await router.handle(event)

            assert mock_adapter.send.call_count == 2
            # Second call should be the rate-limit reminder, not a new code
            reminder_msg: str = mock_adapter.send.call_args_list[1][0][1]
            assert "pending pairing code" in reminder_msg.lower()
