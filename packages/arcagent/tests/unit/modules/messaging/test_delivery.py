"""SPEC-031 D2/REQ-021/REQ-030 — live decorator-path delivery + consume.

Covers the interrupt decision, policy-gated delivery routing into the agent's
run via ``deliver_fn``, signer injection on the messenger, and the verified
consume path used by the inbox loop.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arctrust import AgentIdentity

from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import (
    _handle_incoming,
    _interrupt_for,
    messaging_bind_run_fn,
)
from tests.unit.modules.messaging.conftest import make_config_dict


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _identity() -> AgentIdentity:
    return AgentIdentity.generate(org="local", agent_type="agent")


def _msg(
    *,
    priority: str = "normal",
    action_required: bool = False,
    mentions: list[str] | None = None,
    sender: str = "agent://peer",
    signer_did: str = "did:arc:local:peer/aaaa",
    seq: int = 1,
) -> MagicMock:
    m = MagicMock()
    m.priority = priority
    m.action_required = action_required
    m.mentions = mentions or []
    m.sender = sender
    m.signer_did = signer_did
    m.seq = seq
    m.body = "hello"
    m.msg_type = "info"
    return m


class TestInterruptDecision:
    def test_critical_is_interrupt(self) -> None:
        assert _interrupt_for(_msg(priority="critical"), _identity()) is True

    def test_action_required_mention_is_interrupt(self) -> None:
        ident = _identity()
        msg = _msg(action_required=True, mentions=[ident.did])
        assert _interrupt_for(msg, ident) is True

    def test_action_required_without_mention_is_not_interrupt(self) -> None:
        msg = _msg(action_required=True, mentions=["did:arc:local:other/bbbb"])
        assert _interrupt_for(msg, _identity()) is False

    def test_normal_is_not_interrupt(self) -> None:
        assert _interrupt_for(_msg(), _identity()) is False


class TestBindDeliverFn:
    @pytest.mark.asyncio
    async def test_bind_stores_deliver_fn(self, tmp_path: Path) -> None:
        _runtime.configure(config=make_config_dict(), workspace=tmp_path, identity=_identity())
        run_fn = AsyncMock()
        deliver_fn = AsyncMock()
        ctx = MagicMock()
        ctx.data = {"run_fn": run_fn, "deliver_fn": deliver_fn}
        await messaging_bind_run_fn(ctx)
        st = _runtime.state()
        assert st.agent_run_fn is run_fn
        assert st.deliver_fn is deliver_fn


class TestSignerInjection:
    def test_configure_injects_signer(self, tmp_path: Path) -> None:
        ident = _identity()
        _runtime.configure(config=make_config_dict(), workspace=tmp_path, identity=ident)
        signer = _runtime.state().svc._signer
        assert signer is not None
        assert signer.did == ident.did

    def test_configure_without_identity_has_no_signer(self, tmp_path: Path) -> None:
        _runtime.configure(config=make_config_dict(), workspace=tmp_path, identity=None)
        assert _runtime.state().svc._signer is None


class TestHandleIncoming:
    @pytest.mark.asyncio
    async def test_delivers_via_deliver_fn_with_interrupt_flag(self, tmp_path: Path) -> None:
        """A single pushed message routes through deliver_fn with the steer flag."""
        ident = _identity()
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me"),
            workspace=tmp_path,
            identity=ident,
        )
        st = _runtime.state()
        calls: list[dict[str, Any]] = []

        async def deliver(**kwargs: Any) -> str:
            calls.append(kwargs)
            return "followed_up"

        st.deliver_fn = deliver

        await _handle_incoming(_msg(priority="critical", seq=1))
        await _handle_incoming(_msg(priority="normal", seq=2))

        assert [c["interrupt"] for c in calls] == [True, False]
        assert all(c["session_key"] == "messaging:inbox" for c in calls)
        assert calls[0]["caller_did"] == "did:arc:local:peer/aaaa"

    @pytest.mark.asyncio
    async def test_falls_back_to_agent_run_fn(self, tmp_path: Path) -> None:
        """Before deliver_fn binds, a pushed message still runs via agent_run_fn."""
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me"),
            workspace=tmp_path,
            identity=_identity(),
        )
        st = _runtime.state()
        st.deliver_fn = None
        run_calls: list[str] = []

        async def run_fn(prompt: str, session_key: str = "") -> str:
            run_calls.append(session_key)
            return "ran"

        st.agent_run_fn = run_fn
        await _handle_incoming(_msg())
        assert run_calls == ["messaging:inbox"]


class TestInboxLoopPush:
    @pytest.mark.asyncio
    async def test_subscribe_pushes_verified_message_to_handler(self, tmp_path: Path) -> None:
        """subscribe() pushes a signed, origin-verified bus message into deliver_fn — no poll.

        The message is signed by ``me`` and its ``sender`` resolves to ``me``'s DID, so it
        passes ``_verify_origin`` (signature + sender==signer_did binding). Cross-agent
        peer→peer delivery over real NATS is covered by ``tests/integration/test_spec031_e2e``.
        """
        import asyncio

        from arcteam.types import Message

        from arcagent.modules.messaging import _bootstrap

        ident = _identity()
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me", entity_name="Me"),
            workspace=tmp_path,
            identity=ident,
        )
        st = _runtime.state()
        await st.registry.register(
            _bootstrap.self_entity(
                entity_id="agent://me",
                entity_name="Me",
                handle="me",
                identity=ident,
                roles=[],
                capabilities=[],
            )
        )
        delivered = asyncio.Event()
        bodies: list[str] = []

        async def deliver(**kwargs: Any) -> str:
            bodies.append(kwargs["message"])
            delivered.set()
            return "followed_up"

        st.deliver_fn = deliver

        subscription = await st.svc.subscribe(st.config.entity_id, _handle_incoming)
        try:
            await st.svc.send(Message(sender="agent://me", to=["agent://me"], body="hi"))
            await asyncio.wait_for(delivered.wait(), timeout=3)
        finally:
            await subscription.stop()

        assert any("hi" in b for b in bodies)

    @pytest.mark.asyncio
    async def test_queuefull_raises_retryable_not_dropped(self, tmp_path: Path) -> None:
        """FIX #5: a full steering queue defers redelivery (RetryableDeliveryError), never drops."""
        import asyncio

        from arcteam.messenger import RetryableDeliveryError

        _runtime.configure(
            config=make_config_dict(entity_id="agent://me", entity_name="Me"),
            workspace=tmp_path,
            identity=_identity(),
        )
        st = _runtime.state()

        async def full(**_kwargs: Any) -> str:
            raise asyncio.QueueFull

        st.deliver_fn = full
        with pytest.raises(RetryableDeliveryError):
            await _handle_incoming(_msg(sender="agent://me", signer_did="did:arc:local:me/aaaa"))


class TestEnsureLiveBackend:
    @pytest.mark.asyncio
    async def test_noop_without_url(self, tmp_path: Path) -> None:
        _runtime.configure(config=make_config_dict(), workspace=tmp_path, identity=_identity())
        before = _runtime.state().svc
        await _runtime.ensure_live_backend()
        assert _runtime.state().svc is before
        assert _runtime.state().live_backend_ready is True
