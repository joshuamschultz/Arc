"""End-to-end DM pairing through the REAL adapter → SessionRouter → PairingStore path.

Every other pairing test in this repo (arcgateway's test_session_pairing.py,
test_pairing_dm_delivery.py) hand-builds a SessionRouter/PairingInterceptor
directly with a mock adapter — none of them drive an inbound message through
a real platform adapter's ``_handle_update`` -> ``on_message`` -> real
``TelegramAdapter.send()``. That gap is exactly how the pairing system ended
up "built but dead": every piece worked in isolation, but the wiring between
them (adapter auth gate, PairingInterceptor's ``adapter.send()`` call shape,
cross-process approval) was never proven end-to-end.

This test drives the actual sequence a live deployment goes through:
    1. An unknown Telegram user messages the bot (require_pairing=true).
    2. TelegramAdapter forwards to SessionRouter.handle (real router, real
       PairingInterceptor, real PairingStore backed by a temp SQLite file).
    3. PairingInterceptor mints a code and calls the REAL TelegramAdapter.send()
       — this is what caught the DeliveryTarget/chat_id shape mismatch that
       every mock-adapter test missed.
    4. A separate PairingStore instance (simulating the `arc gateway pair
       approve` CLI process) approves the code against the SAME db_path.
    5. The user sends a second message — it must now reach the agent
       executor, proving the live gateway sees a cross-process approval with
       no in-memory allowlist involved.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arcgateway.executor import Delta, InboundEvent
from arcgateway.pairing import PairingStore
from arcgateway.session import SessionRouter

from arcgateway_telegram.adapter import TelegramAdapter


def _make_mock_application() -> MagicMock:
    """Minimal mocked python-telegram-bot Application — send() only."""
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    return app


def _make_update(user_id: int, text: str, update_id: int = 1) -> MagicMock:
    update = MagicMock()
    update.update_id = update_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = 5555
    update.effective_message = MagicMock()
    update.effective_message.text = text
    return update


class _RecordingExecutor:
    """Executor stand-in that records every event it's asked to run."""

    def __init__(self) -> None:
        self.calls: list[InboundEvent] = []

    async def run(self, event: InboundEvent) -> Any:
        self.calls.append(event)

        async def _stream() -> Any:
            yield Delta(kind="done", content="ok", is_final=True, turn_id=event.session_key)

        return _stream()


@pytest.mark.asyncio
async def test_unpaired_telegram_user_gets_code_then_approval_routes_through(
    tmp_path: Path,
) -> None:
    """Full real-adapter E2E: unknown user -> minted code DM -> approve -> routed."""
    db_path = tmp_path / "pairing.db"
    executor = _RecordingExecutor()

    # The live gateway's own PairingStore + SessionRouter + TelegramAdapter —
    # exactly as GatewayRunner.from_config wires them when require_pairing=true.
    daemon_store = PairingStore(db_path=db_path, tier="personal")
    router = SessionRouter(executor=executor, pairing_store=daemon_store)  # type: ignore[arg-type]

    adapter = TelegramAdapter(
        bot_token="test-token",
        allowed_user_ids=[],  # nobody statically allowlisted
        on_message=router.handle,
        agent_did="did:arc:agent:test",
        require_pairing=True,
    )
    adapter._bot_id = 999
    router.register_adapter(adapter)
    mock_app = _make_mock_application()
    adapter._application = mock_app

    # Step 1+2+3: unknown user messages the bot.
    update = _make_update(user_id=42, text="hello agent")
    await adapter._handle_update(update, context=MagicMock())

    # The agent must NOT have been invoked.
    assert executor.calls == []
    # A pairing-code DM must have been sent through the REAL send() path
    # (DeliveryTarget, not a raw chat_id string).
    mock_app.bot.send_message.assert_called_once()
    sent_text = mock_app.bot.send_message.call_args.kwargs["text"]
    assert "arc gateway pair approve" in sent_text

    code = next(word for word in sent_text.split() if len(word) == 8 and word.isalnum())

    # Step 4: a SEPARATE process (the CLI) approves against the same db_path.
    from arcgateway.pairing import build_pairing_challenge
    from nacl.signing import SigningKey

    trust_dir = tmp_path / "trust"
    trust_dir.mkdir()
    sk = SigningKey.generate()
    import base64

    from arctrust import invalidate_cache

    did = "did:arc:org:operator/e2e-test"
    (trust_dir / "operators.toml").write_text(
        f'[operators."{did}"]\npublic_key = "{base64.b64encode(bytes(sk.verify_key)).decode()}"\n',
        encoding="utf-8",
    )
    (trust_dir / "operators.toml").chmod(0o600)
    invalidate_cache()

    cli_store = PairingStore(db_path=db_path, tier="personal", trust_dir=trust_dir)
    pending = await cli_store.list_pending()
    matched = next(pc for pc in pending if pc.code == code)
    challenge = build_pairing_challenge(matched.code, matched.minted_at)
    signature = bytes(sk.sign(challenge).signature)
    approved = await cli_store.verify_and_consume(code, approver_did=did, signature=signature)
    assert approved is not None

    # Step 5: the same user messages again — must now reach the agent.
    update2 = _make_update(user_id=42, text="second message")
    await adapter._handle_update(update2, context=MagicMock())
    await asyncio.sleep(0.05)  # SessionRouter.handle spawns a task

    assert len(executor.calls) == 1
    assert executor.calls[0].user_did == "did:arc:telegram:42"
