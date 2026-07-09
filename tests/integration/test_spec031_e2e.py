"""SPEC-031 acceptance: the §0 demo end-to-end over a real nats-server.

This is the single integration test that proves the ArcTeam coordination
plumbing works front-to-back WITHOUT a live LLM. It exercises, over a real
``nats-server`` with JetStream:

1. Registration of three entities (researcher / builder / critic), each with a
   real ``arctrust`` DID + ``@handle``, plus a :class:`arcteam.Team`.
2. A signed ``@builder`` message from researcher: handle -> DID resolution,
   signature verification on receive, mention recording, and raised attention
   flags (``action_required`` + priority >= HIGH).
3. A *running* durable subscriber for builder receiving that message **pushed**
   (no poll call by the test) within a timeout.
4. The delivered message driven through the real ``ArcAgent.deliver_message``
   decision path with a stub RunHandle and the real ``arctrust`` policy
   pipeline: normal -> ``follow_up``; action-required mention + permissive
   policy -> ``steer``; denied policy -> ``follow_up`` (fail-safe degrade).
5. Security quarantine: a tampered message -> DLQ ``bad_signature``; a duplicated
   message is deduped and delivered exactly once on the push path (explicit
   nonce-window replay -> DLQ ``replay`` is the ``receive()`` path's unit tests).

Only the LLM/run execution is stubbed (a fake handle records steer/follow_up).
Everything else — arcteam, arctrust identity/crypto/policy, and the NATS
transport — is real.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncGenerator, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
from arcagent.core.agent import ArcAgent
from arcteam.audit import AuditLogger
from arcteam.crypto import MessageSigner, new_nonce, sign_message, verify_message
from arcteam.messenger import MessagingService, Subscription
from arcteam.registry import EntityRegistry, resolve
from arcteam.storage import StorageBackend
from arcteam.team import Team, TeamStore
from arcteam.types import Entity, EntityType, Message, Priority, generate_message_id
from arctrust import AgentIdentity, generate_did, generate_keypair
from arctrust.signer import InProcessSigner
from nacl.signing import SigningKey

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server not installed"),
]

STREAMS = "messages/streams"
SESSION_KEY = "skunkworks"


# --------------------------------------------------------------------------- #
# Real nats-server with JetStream (module-scoped process).
# --------------------------------------------------------------------------- #


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def server_url() -> Iterator[str]:
    port = _free_port()
    store = tempfile.mkdtemp(prefix="arcteam-e2e-")
    proc = subprocess.Popen(
        ["nats-server", "-js", "-p", str(port), "-sd", store],  # noqa: S607  # dev tool via PATH, guarded by shutil.which
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        proc.terminate()
        pytest.fail("nats-server did not start")
    try:
        yield f"nats://127.0.0.1:{port}"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
        shutil.rmtree(Path(store), ignore_errors=True)


@pytest.fixture
async def backend(server_url: str) -> AsyncGenerator[StorageBackend, None]:
    from arcteam.backends.nats import NatsBackend

    be = await NatsBackend.connect(server_url)
    try:
        yield be
    finally:
        await be.close()


# --------------------------------------------------------------------------- #
# Entity + identity fixtures (real arctrust DIDs / keypairs).
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Member:
    """A registered team member with its real signing identity."""

    handle: str
    identity: AgentIdentity
    signer: MessageSigner
    service: MessagingService

    @property
    def did(self) -> str:
        return self.identity.did

    @property
    def uri(self) -> str:
        return f"agent://{self.handle}"


def _make_identity(handle: str) -> tuple[AgentIdentity, MessageSigner]:
    """Mint a real Ed25519 identity and a matching message signer for ``handle``."""
    keypair = generate_keypair()
    signing_key = SigningKey(keypair.private_key)
    did = generate_did(signing_key.verify_key, org="arc", agent_type="executor")
    identity = AgentIdentity(did=did, public_key=keypair.public_key, _signing_key=signing_key)
    signer = MessageSigner(did=did, private_key=keypair.private_key)
    return identity, signer


async def _register_member(
    handle: str,
    backend: StorageBackend,
    registry: EntityRegistry,
    audit: AuditLogger,
) -> _Member:
    """Register ``handle`` as a DID-keyed entity with its own signed messenger."""
    identity, signer = _make_identity(handle)
    await registry.register(
        Entity(
            did=identity.did,
            handle=handle,
            id=f"agent://{handle}",
            name=handle.title(),
            type=EntityType.AGENT,
            public_key=identity.public_key.hex(),
        )
    )
    service = MessagingService(backend, registry, audit, signer=signer)
    return _Member(handle=handle, identity=identity, signer=signer, service=service)


# --------------------------------------------------------------------------- #
# Stub RunHandle + agent harness — stubs ONLY the LLM/run execution.
# --------------------------------------------------------------------------- #


@dataclass
class _FakeRunHandle:
    """Records steer/follow_up injections instead of running a real loop."""

    steered: list[tuple[str, str]] = field(default_factory=list)
    followed_up: list[tuple[str, str]] = field(default_factory=list)

    async def steer(self, caller_did: str, message: str) -> None:
        self.steered.append((caller_did, message))

    async def follow_up(self, caller_did: str, message: str) -> None:
        self.followed_up.append((caller_did, message))


@dataclass
class _RecordingTelemetry:
    """Captures audit events so the deny path can be asserted."""

    events: list[tuple[str, dict[str, object]]] = field(default_factory=list)

    def audit_event(self, name: str, payload: dict[str, object]) -> None:
        self.events.append((name, payload))


class _StubAgent:
    """Minimal stand-in that runs the REAL ArcAgent steering decision path.

    ``deliver_message`` and ``_authorize_steer`` delegate to the actual
    ``ArcAgent`` methods, so the real branching and the real ``arctrust``
    policy evaluation execute verbatim. Only the run-execution surface
    (``_ensure_started`` / ``start_tracked_run`` / the RunHandle) is stubbed.
    """

    def __init__(
        self,
        *,
        identity: AgentIdentity,
        pipeline: object,
        telemetry: _RecordingTelemetry,
        handle: _FakeRunHandle,
        tier: str = "personal",
    ) -> None:
        self._identity = identity
        self._policy_pipeline = pipeline
        self._telemetry = telemetry
        self._active_runs = {SESSION_KEY: handle}
        self._config = SimpleNamespace(security=SimpleNamespace(tier=tier))
        self.started_runs: list[tuple[str, str]] = []

    def _ensure_started(self) -> None:
        """No-op guard: the run subsystem is stubbed, not started."""

    async def start_tracked_run(self, message: str, *, session_key: str) -> None:
        self.started_runs.append((session_key, message))

    async def _authorize_steer(self, caller_did: str) -> bool:
        return await ArcAgent._authorize_steer(cast(ArcAgent, self), caller_did)

    def _audit_steer_denied(
        self, caller_did: str, *, layer: str | None, rule_id: str | None, reason: str | None
    ) -> None:
        ArcAgent._audit_steer_denied(
            cast(ArcAgent, self), caller_did, layer=layer, rule_id=rule_id, reason=reason
        )

    async def deliver(self, *, caller_did: str, message: str, interrupt: bool) -> str:
        return await ArcAgent.deliver_message(
            cast(ArcAgent, self),
            caller_did=caller_did,
            message=message,
            session_key=SESSION_KEY,
            interrupt=interrupt,
        )


def _should_interrupt(message: Message) -> bool:
    """The inbox interrupt rule (REQ-041): critical priority or a live mention."""
    return message.priority == Priority.CRITICAL or (
        message.action_required and bool(message.mentions)
    )


async def _wait_for_dlq_reason(
    service: MessagingService, reason: str, *, timeout: float = 5.0
) -> bool:
    """Poll the DLQ until an entry with ``reason`` appears or the timeout lapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        dlq = await service.dlq_list()
        if any(entry["meta"].get("dlq_reason") == reason for entry in dlq):
            return True
        await asyncio.sleep(0.05)
    return False


# --------------------------------------------------------------------------- #
# The one end-to-end acceptance test.
# --------------------------------------------------------------------------- #


async def test_spec031_acceptance_flow(backend: StorageBackend) -> None:
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)

    # 1. Register three entities with real DIDs + handles, and form a team.
    researcher = await _register_member("researcher", backend, registry, audit)
    builder = await _register_member("builder", backend, registry, audit)
    critic = await _register_member("critic", backend, registry, audit)

    team_store = TeamStore(backend, audit)
    team = await team_store.create(
        Team(
            id="skunkworks",
            name="Skunkworks",
            members=[researcher.did, builder.did, critic.did],
            default_channel="channel://skunkworks",
        )
    )
    assert set(team.members) == {researcher.did, builder.did, critic.did}

    # 3. Start builder's running push subscriber BEFORE the send.
    delivered: list[Message] = []
    pushed = asyncio.Event()

    async def builder_inbox(message: Message) -> None:
        delivered.append(message)
        pushed.set()

    subscription: Subscription = await builder.service.subscribe(builder.uri, builder_inbox)

    # 2. researcher sends a SIGNED message addressed by @builder, mentioning @builder.
    sent = await researcher.service.send(
        Message(
            sender=researcher.uri,
            to=["@builder"],
            body="@builder please review the coordination plan",
        )
    )
    # Handle -> DID resolution is the single resolver path (REQ-002).
    assert await resolve(registry, "@builder") == builder.did
    # Mention recorded + attention raised (REQ-004).
    assert sent.mentions == [builder.did]
    assert sent.action_required is True
    assert sent.priority == Priority.HIGH
    assert sent.signer_did == researcher.did

    # 3 (cont). The message is PUSHED to the running subscriber — no poll here.
    try:
        await asyncio.wait_for(pushed.wait(), timeout=10)
    finally:
        await subscription.stop()
    assert len(delivered) == 1
    inbound = delivered[0]
    assert inbound.body == "@builder please review the coordination plan"
    assert inbound.mentions == [builder.did]
    # Signature verifies on receive against researcher's registered key (REQ-030).
    assert verify_message(inbound, researcher.identity.public_key) is True
    assert _should_interrupt(inbound) is True

    # 4. Drive the delivered message through the REAL deliver_message decision path.
    permissive = _build_pipeline(builder.identity, deny=False)
    denied = _build_pipeline(builder.identity, deny=True)

    # 4a. A normal (non-interrupting) message -> follow_up.
    normal_handle = _FakeRunHandle()
    normal_agent = _StubAgent(
        identity=builder.identity,
        pipeline=permissive,
        telemetry=_RecordingTelemetry(),
        handle=normal_handle,
    )
    normal_msg = Message(sender=researcher.uri, to=["@builder"], body="status update, no rush")
    action_normal = await normal_agent.deliver(
        caller_did=researcher.did, message=normal_msg.body, interrupt=_should_interrupt(normal_msg)
    )
    assert action_normal == "followed_up"
    assert normal_handle.followed_up == [(researcher.did, normal_msg.body)]
    assert normal_handle.steered == []

    # 4b. The action-required mention + a permissive policy -> steer (mid-turn).
    steer_handle = _FakeRunHandle()
    steer_agent = _StubAgent(
        identity=builder.identity,
        pipeline=permissive,
        telemetry=_RecordingTelemetry(),
        handle=steer_handle,
    )
    action_steer = await steer_agent.deliver(
        caller_did=researcher.did, message=inbound.body, interrupt=_should_interrupt(inbound)
    )
    assert action_steer == "steered"
    assert steer_handle.steered == [(researcher.did, inbound.body)]
    assert steer_handle.followed_up == []

    # 4c. Same critical mention but a DENYING policy -> fail-safe degrade to follow_up.
    deny_handle = _FakeRunHandle()
    deny_telemetry = _RecordingTelemetry()
    deny_agent = _StubAgent(
        identity=builder.identity,
        pipeline=denied,
        telemetry=deny_telemetry,
        handle=deny_handle,
    )
    action_denied = await deny_agent.deliver(
        caller_did=researcher.did, message=inbound.body, interrupt=_should_interrupt(inbound)
    )
    assert action_denied == "followed_up"
    assert deny_handle.followed_up == [(researcher.did, inbound.body)]
    assert deny_handle.steered == []
    assert any(name == "messaging.steer.denied" for name, _ in deny_telemetry.events)

    # 5a. A tampered message is quarantined -> DLQ bad_signature (never delivered).
    tampered: list[Message] = []

    async def critic_inbox(message: Message) -> None:
        tampered.append(message)

    honest = _sign(researcher, [critic.uri], "signed and honest")
    assert verify_message(honest, researcher.identity.public_key) is True
    corrupted = honest.model_dump()
    corrupted["body"] = "rewritten by an attacker"  # body is signature-bound → verify fails
    await backend.append_auto_seq(STREAMS, "arc.agent.critic", corrupted)

    tamper_sub = await critic.service.subscribe(critic.uri, critic_inbox)
    try:
        assert await _wait_for_dlq_reason(critic.service, "bad_signature")
    finally:
        await tamper_sub.stop()
    assert tampered == []

    # 5b. Exactly-once under duplication (the push path's replay guarantee).
    # Send a valid message, then re-inject the identical record so the stream
    # carries the same message id twice; a running subscriber delivers the first
    # copy and silently dedups the second (id-dedup), never delivering it again.
    # (Explicit nonce-window replay -> DLQ ``replay`` is the ``receive()`` path,
    # covered by arcteam's unit tests.)
    replay_delivered: list[Message] = []
    replay_seen = asyncio.Event()

    async def builder_replay_inbox(message: Message) -> None:
        replay_delivered.append(message)
        if message.body == "replay me":
            replay_seen.set()

    await researcher.service.send(
        Message(sender=researcher.uri, to=[builder.uri], body="replay me")
    )
    records = await backend.read_stream(STREAMS, "arc.agent.builder")
    duplicate = records[-1]
    await backend.append_auto_seq(STREAMS, "arc.agent.builder", duplicate)

    # A fresh service (clean replay window) + a fresh durable that reads the
    # stream from the start, so both copies of the nonce reach the verifier.
    probe = MessagingService(backend, registry, audit, signer=builder.signer)
    replay_sub = await probe.subscribe(
        builder.uri, builder_replay_inbox, durable_name="builder-replay-probe"
    )
    try:
        await asyncio.wait_for(replay_seen.wait(), timeout=10)
        # Both copies are on the stream; give the loop time to consume the
        # duplicate and confirm it is deduped (not delivered a second time).
        await asyncio.sleep(1.0)
    finally:
        await replay_sub.stop()
    # The honest "replay me" was delivered exactly once; the duplicate was deduped.
    assert [m.body for m in replay_delivered].count("replay me") == 1


def _sign(member: _Member, to: list[str], body: str) -> Message:
    """Build a fully signed message envelope (as ``send`` would) without routing it.

    Used to inject an adversarially altered record straight onto the stream:
    the signature binds the body, so corrupting it after signing fails verify.
    """
    message = Message(
        id=generate_message_id(),
        ts=datetime.now(UTC).isoformat(),
        sender=member.uri,
        to=to,
        body=body,
        signer_did=member.did,
        nonce=new_nonce(),
    )
    sign_message(message, member.signer.private_key)
    return message


def _build_pipeline(identity: AgentIdentity, *, deny: bool) -> object:
    """A personal-tier arctrust pipeline; ``deny`` blocks the steer tool.

    Uses the real ``arctrust.build_pipeline`` so ``_authorize_steer`` runs the
    genuine identity + global policy layers, not a mock.
    """
    from arctrust.policy import build_pipeline

    deny_rules = {"messaging_steer": "steering disabled by policy"} if deny else {}
    return build_pipeline(
        tier="personal",
        agent_registry={identity.did: identity.public_key},
        global_deny_rules=deny_rules,
    )
