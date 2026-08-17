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
from arctrust.session_identity import build_session_key
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import (
    _handle_incoming,
    _interrupt_for,
    messaging_bind_run_fn,
)


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _identity() -> AgentIdentity:
    return AgentIdentity.generate(org="local", agent_type="agent")


async def _register_human(st: Any, did: str) -> None:
    """Register ``did`` as a ``user`` entity so its channel posts fan out."""
    from arcteam.types import Entity, EntityType

    await st.registry.register(
        Entity(
            did=did,
            handle="operator",
            id="user://operator",
            name="Operator",
            type=EntityType.USER,
        )
    )


def _msg(
    *,
    priority: str = "normal",
    action_required: bool = False,
    mentions: list[str] | None = None,
    sender: str = "agent://peer",
    signer_did: str = "did:arc:local:peer/aaaa",
    seq: int = 1,
    to: list[str] | None = None,
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
    m.to = to if to is not None else ["agent://me"]
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
        _runtime.configure(
            config=make_config_dict(),
            workspace=tmp_path,
            identity=_identity(),
            operator_signer=make_operator_signer(),
        )
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
        _runtime.configure(
            config=make_config_dict(),
            workspace=tmp_path,
            identity=ident,
            operator_signer=make_operator_signer(),
        )
        signer = _runtime.state().svc._signer
        assert signer is not None
        assert signer.did == ident.did

    def test_configure_without_identity_has_no_signer(self, tmp_path: Path) -> None:
        _runtime.configure(
            config=make_config_dict(),
            workspace=tmp_path,
            identity=None,
            operator_signer=make_operator_signer(),
        )
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
            operator_signer=make_operator_signer(),
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
        # One session per (this agent, that sender), derived by the session
        # identity owner — the same key a human on a surface would get. A shared
        # constant would put every teammate's traffic in one context (REQ-312).
        expected_key = build_session_key(ident.did, "did:arc:local:peer/aaaa")
        assert all(c["session_key"] == expected_key for c in calls)
        assert calls[0]["caller_did"] == "did:arc:local:peer/aaaa"

    @pytest.mark.asyncio
    async def test_channel_post_threads_channel_as_reply_target(self, tmp_path: Path) -> None:
        """A channel post binds that channel as the turn's reply target, so a
        reply (notify_user / schedule) returns to the channel it came from."""
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me"),
            workspace=tmp_path,
            identity=_identity(),
            operator_signer=make_operator_signer(),
        )
        st = _runtime.state()
        calls: list[dict[str, Any]] = []

        async def deliver(**kwargs: Any) -> str:
            calls.append(kwargs)
            return "started"

        st.deliver_fn = deliver
        # An un-addressed channel post only fans out when a human wrote it
        # (SPEC-068 D4a), so the sender has to be a registered one. Routing is
        # off because the subject here is the reply target, not who answers.
        await _register_human(st, "did:arc:local:peer/aaaa")
        st.config = st.config.model_copy(update={"channel_route": False})

        await _handle_incoming(_msg(to=["channel://ops"], mentions=[]))

        assert calls[0]["reply_target"] == "channel://ops"
        assert calls[0]["reply_label"] == "Channel — ops"

    @pytest.mark.asyncio
    async def test_direct_message_threads_no_reply_target(self, tmp_path: Path) -> None:
        """A teammate DM keeps reply_target None — notify_user still reaches the
        human on their own channel, not the teammate."""
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me"),
            workspace=tmp_path,
            identity=_identity(),
            operator_signer=make_operator_signer(),
        )
        st = _runtime.state()
        calls: list[dict[str, Any]] = []

        async def deliver(**kwargs: Any) -> str:
            calls.append(kwargs)
            return "started"

        st.deliver_fn = deliver

        await _handle_incoming(_msg(to=["agent://me"]))

        assert calls[0]["reply_target"] is None

    @pytest.mark.asyncio
    async def test_two_senders_get_two_sessions(self, tmp_path: Path) -> None:
        """Different teammates never share a session (REQ-312)."""
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me"),
            workspace=tmp_path,
            identity=_identity(),
            operator_signer=make_operator_signer(),
        )
        st = _runtime.state()
        calls: list[dict[str, Any]] = []

        async def deliver(**kwargs: Any) -> str:
            calls.append(kwargs)
            return "followed_up"

        st.deliver_fn = deliver

        await _handle_incoming(_msg(signer_did="did:arc:local:peer/aaaa"))
        await _handle_incoming(_msg(signer_did="did:arc:local:peer/bbbb"))

        assert len({c["session_key"] for c in calls}) == 2

    @pytest.mark.asyncio
    async def test_falls_back_to_agent_run_fn(self, tmp_path: Path) -> None:
        """Before deliver_fn binds, a pushed message still runs via agent_run_fn."""
        ident = _identity()
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me"),
            workspace=tmp_path,
            identity=ident,
            operator_signer=make_operator_signer(),
        )
        st = _runtime.state()
        st.deliver_fn = None
        run_calls: list[str] = []

        async def run_fn(prompt: str, session_key: str = "", **_kwargs: Any) -> str:
            run_calls.append(session_key)
            return "ran"

        st.agent_run_fn = run_fn
        await _handle_incoming(_msg())
        assert run_calls == [build_session_key(ident.did, "did:arc:local:peer/aaaa")]


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

        from arcagent.core import arcteam_bootstrap as _bootstrap

        ident = _identity()
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me", entity_name="Me"),
            workspace=tmp_path,
            identity=ident,
            operator_signer=make_operator_signer(),
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
        pushed: list[Any] = []

        async def deliver(**kwargs: Any) -> str:
            bodies.append(kwargs["message"])
            return "followed_up"

        st.deliver_fn = deliver

        async def handler(message: Any) -> None:
            pushed.append(message)
            delivered.set()
            await _handle_incoming(message)

        subscription = await st.svc.subscribe(st.config.entity_id, handler)
        try:
            await st.svc.send(Message(sender="agent://me", to=["agent://me"], body="hi"))
            await asyncio.wait_for(delivered.wait(), timeout=3)
        finally:
            await subscription.stop()

        assert [m.body for m in pushed] == ["hi"], "subscribe did not push to the handler"
        # The only sender that can be signed here is this agent itself, and an
        # agent never activates on its own message (SPEC-068 D4c) — otherwise a
        # post that @mentions itself re-wakes the run that wrote it. So the push
        # is asserted at the handler, and the suppression at deliver_fn.
        assert bodies == []

    @pytest.mark.asyncio
    async def test_queuefull_raises_retryable_not_dropped(self, tmp_path: Path) -> None:
        """FIX #5: a full steering queue defers redelivery (RetryableDeliveryError), never drops."""
        import asyncio

        from arcteam.messenger import RetryableDeliveryError

        _runtime.configure(
            config=make_config_dict(entity_id="agent://me", entity_name="Me"),
            workspace=tmp_path,
            identity=_identity(),
            operator_signer=make_operator_signer(),
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
        _runtime.configure(
            config=make_config_dict(),
            workspace=tmp_path,
            identity=_identity(),
            operator_signer=make_operator_signer(),
        )
        before = _runtime.state().svc
        await _runtime.ensure_live_backend()
        assert _runtime.state().svc is before
        assert _runtime.state().live_backend_ready is True
