"""Alpha-2 item 3 acceptance: mail is answered, once, over a real nats-server.

The reported defect: agents never answered inbox mail. The mail woke a turn, the
turn produced an answer, and the answer was dropped.

What is real here: a ``nats-server`` with JetStream, arcteam's signed
``MessagingService`` (Ed25519 sign + verify + replay window), the entity
registry, ``AgentMailService`` with the one-reply rule, the durable inbox
projection (in-memory repository) and its outbox, and the agent's real
messaging module — ``_handle_incoming`` (admission, activation, wake prompt)
and the ``agent:post_respond`` finalizer that posts the reply.

What is scripted: only the model. Each agent's run is a scripted turn that
records the wake prompt it was given and finishes with a fixed final text, the
way the LLM wire would. Everything between the bus and that text is real.

Scenarios:

1. Operator composes mail -> the agent is woken with a prompt naming
   ``@operator``, the subject and the thread -> its answer is the thread's one
   reply, visible in the operator's copy.
2. An FYI (scripted empty final text) is finished silently: no reply.
3. Two agents: alpha asks beta; beta answers; the answer wakes alpha, whose
   reply attempt is refused by the one-reply rule — the exchange stops.
4. Handoff round trip: the operator hands a thread to the agent; the durable
   handoff wakes the agent by signed mail; the handoff is then accepted.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcagent.core import turn_context
from arcagent.core.module_bus import EventContext
from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import _handle_incoming, deliver_origin_reply
from arcstore.inbox import HandoffStatus, Participant, ParticipantRole, TraceMetadata
from arcstore.inbox_projection import DurableInboxService, thread_id_for
from arcstore.mail_outbox import MailOutbox
from arcteam.audit import AuditLogger
from arcteam.crypto import MessageSigner
from arcteam.mail import (
    AgentMailService,
    MailInboxDeliveryPort,
    MailSendRequest,
    MailThreadClosedError,
    RegistryMailAddressBook,
)
from arcteam.messenger import MessagingService
from arcteam.registry import EntityRegistry
from arcteam.storage import StorageBackend
from arcteam.types import Entity, EntityType
from arctrust import AgentIdentity, OperatorKey
from arctrust.signer import InProcessSigner

from packages.arcstore.tests.unit.inbox_fake import FakeInboxRepository

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(shutil.which("nats-server") is None, reason="nats-server not installed"),
]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port: int = sock.getsockname()[1]
        return port


def _start_server(port: int, store: str) -> subprocess.Popen[bytes] | None:
    proc = subprocess.Popen(
        ["nats-server", "-js", "-p", str(port), "-sd", store],  # noqa: S607  # dev tool via PATH, guarded by shutil.which above
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return proc
        except OSError:
            time.sleep(0.05)
    proc.terminate()
    proc.wait(timeout=5)
    return None


@pytest.fixture
def server_url() -> Iterator[str]:
    store = tempfile.mkdtemp(prefix="arcteam-mail-e2e-")
    proc: subprocess.Popen[bytes] | None = None
    port = 0
    for _attempt in range(3):
        port = _free_port()
        proc = _start_server(port, store)
        if proc is not None:
            break
    if proc is None:
        shutil.rmtree(Path(store), ignore_errors=True)
        pytest.skip("nats-server would not bind a free port after 3 attempts")
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


class _OutboxRepository(FakeInboxRepository):
    """The in-memory repository plus the atomic outbox write the real one performs."""

    def __init__(self, outbox: MailOutbox) -> None:
        super().__init__()
        self._outbox = outbox

    async def record_event_with_outbox(self, **kwargs: Any) -> Any:
        copies = await super().record_event_with_outbox(**kwargs)
        self._outbox.enqueue(str(kwargs["event_id"]), dict(kwargs["envelope"]))
        return copies


@dataclass
class _Fleet:
    backend: StorageBackend
    registry: EntityRegistry
    audit: AuditLogger
    inbox: DurableInboxService
    outbox: MailOutbox
    port: MailInboxDeliveryPort

    def mail(self, identity: AgentIdentity) -> AgentMailService:
        signer = MessageSigner.from_identity(identity)
        return AgentMailService(
            MessagingService(self.backend, self.registry, self.audit, signer=signer),
            self.inbox,
            outbox=self.outbox,
            address_book=RegistryMailAddressBook(self.registry),
            signer=signer,
        )

    async def register(self, identity: AgentIdentity, handle: str, kind: EntityType) -> None:
        scheme = "agent" if kind is EntityType.AGENT else "user"
        await self.registry.register(
            Entity(
                did=identity.did,
                handle=handle,
                id=f"{scheme}://{handle}",
                name=handle.title(),
                type=kind,
                public_key=identity.public_key.hex(),
            )
        )


@pytest.fixture
async def fleet(backend: StorageBackend, tmp_path: Path) -> _Fleet:
    audit = AuditLogger(backend, InProcessSigner(b"\x11" * 32))
    await audit.initialize()
    outbox = MailOutbox(tmp_path / "mail-outbox.jsonl")
    port = MailInboxDeliveryPort()
    return _Fleet(
        backend=backend,
        registry=EntityRegistry(backend, audit),
        audit=audit,
        inbox=DurableInboxService(_OutboxRepository(outbox), delivery_port=port),
        outbox=outbox,
        port=port,
    )


@dataclass
class _ScriptedAgent:
    """One agent: the real messaging module, with only the model scripted."""

    handle: str
    identity: AgentIdentity
    replies: list[str]
    prompts: list[str] = field(default_factory=list)
    turns: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    task: asyncio.Task[None] | None = None
    stop: asyncio.Event = field(default_factory=asyncio.Event)


async def _run_agent(agent: _ScriptedAgent, fleet: _Fleet, workspace: Path) -> None:
    """Configure the agent's messaging runtime in its own context and serve its inbox."""
    _runtime.configure(
        config={
            "enabled": True,
            "entity_id": f"agent://{agent.handle}",
            "entity_name": agent.handle,
        },
        workspace=workspace,
        identity=agent.identity,
        operator_signer=OperatorKey.generate().into_signer(),
    )
    st = _runtime.state()
    mail = fleet.mail(agent.identity)
    st.svc = MessagingService(
        fleet.backend,
        fleet.registry,
        fleet.audit,
        signer=MessageSigner.from_identity(agent.identity),
    )
    st.reply_port = st.svc
    st.registry = fleet.registry
    st.mail_service = mail
    st.arcstore_opener = object()
    run = 0

    async def scripted_turn(**kwargs: Any) -> None:
        nonlocal run
        run += 1
        agent.prompts.append(str(kwargs["message"]))
        # The dispatch entry binds the reply target; the finalizer reads it.
        turn_context.set_inbound_channel(kwargs["reply_target"])
        final_text = agent.replies.pop(0) if agent.replies else ""
        await deliver_origin_reply(
            EventContext(
                event="agent:post_respond",
                data={
                    "messages": [
                        {"role": "user", "content": kwargs["message"]},
                        {"role": "assistant", "content": final_text},
                    ],
                    "session_id": kwargs["session_key"],
                    "run_id": f"{agent.handle}-run-{run}",
                    "automated": True,
                },
                agent_did=agent.identity.did,
                trace_id="e2e",
            )
        )
        await agent.turns.put(final_text)

    st.deliver_fn = scripted_turn
    subscription = await st.svc.subscribe(f"agent://{agent.handle}", _handle_incoming)
    try:
        await agent.stop.wait()
    finally:
        await subscription.stop()


async def _start(agent: _ScriptedAgent, fleet: _Fleet, workspace: Path) -> None:
    workspace.mkdir(parents=True, exist_ok=True)
    agent.task = asyncio.create_task(_run_agent(agent, fleet, workspace))
    await asyncio.sleep(0.3)


async def _stop(*agents: _ScriptedAgent) -> None:
    for agent in agents:
        agent.stop.set()
    await asyncio.gather(*(a.task for a in agents if a.task is not None))


async def _eventually(check: Callable[[], Awaitable[bool]], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if await check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError("condition not met before timeout")


def _human(identity: AgentIdentity) -> Participant:
    return Participant(participant_id=identity.did, role=ParticipantRole.HUMAN)


def _agent(identity: AgentIdentity) -> Participant:
    return Participant(participant_id=identity.did, role=ParticipantRole.AGENT)


async def _bodies(fleet: _Fleet, owner: Participant, conversation: str) -> list[str]:
    page = await fleet.inbox.list_messages(
        thread_id_for(owner.participant_id, conversation), reader=owner
    )
    return [item.body for item in page.items]


async def _compose(
    fleet: _Fleet, operator: AgentIdentity, to: AgentIdentity, subject: str, body: str
) -> str:
    sent = await fleet.mail(operator).send(
        MailSendRequest(
            sender="user://operator",
            sender_did=operator.did,
            to=(to.did,),
            subject=subject,
            body=body,
            idempotency_key=f"compose:{subject}",
        )
    )
    assert sent.status == "sent"
    return sent.thread_id


async def test_operator_mail_is_answered_once_in_the_same_thread(
    fleet: _Fleet, tmp_path: Path
) -> None:
    operator = AgentIdentity.generate("arc", "operator")
    beta = _ScriptedAgent(
        "beta", AgentIdentity.generate("arc", "executor"), ["Q3 revenue was 4.2M."]
    )
    await fleet.register(operator, "operator", EntityType.USER)
    await fleet.register(beta.identity, "beta", EntityType.AGENT)
    await _start(beta, fleet, tmp_path / "beta")
    try:
        conversation = await _compose(
            fleet, operator, beta.identity, "Q3 numbers", "What was Q3 revenue?"
        )

        await asyncio.wait_for(beta.turns.get(), timeout=10)
        await _eventually(lambda: _has(fleet, _human(operator), conversation, 2))

        assert await _bodies(fleet, _human(operator), conversation) == [
            "What was Q3 revenue?",
            "Q3 revenue was 4.2M.",
        ]
        prompt = beta.prompts[0]
        assert "@operator" in prompt
        assert "Q3 numbers" in prompt
        assert conversation in prompt
        with pytest.raises(MailThreadClosedError):
            await fleet.mail(operator).reply_to_conversation(
                conversation, body="And Q4?", idempotency_key="follow-up"
            )

        fyi = await _compose(
            fleet, operator, beta.identity, "Deploy done", "FYI: deploy finished."
        )
        assert await asyncio.wait_for(beta.turns.get(), timeout=10) == ""
        await asyncio.sleep(0.3)
        assert await _bodies(fleet, _human(operator), fyi) == ["FYI: deploy finished."]
    finally:
        await _stop(beta)


async def _has(fleet: _Fleet, owner: Participant, conversation: str, count: int) -> bool:
    try:
        return len(await _bodies(fleet, owner, conversation)) >= count
    except KeyError:
        return False


async def test_two_agents_stop_after_the_one_reply(fleet: _Fleet, tmp_path: Path) -> None:
    alpha = _ScriptedAgent("alpha", AgentIdentity.generate("arc", "executor"), ["Thanks, and Q4?"])
    beta = _ScriptedAgent("beta", AgentIdentity.generate("arc", "executor"), ["Q3 was 4.2M."])
    await fleet.register(alpha.identity, "alpha", EntityType.AGENT)
    await fleet.register(beta.identity, "beta", EntityType.AGENT)
    await _start(alpha, fleet, tmp_path / "alpha")
    await _start(beta, fleet, tmp_path / "beta")
    try:
        sent = await fleet.mail(alpha.identity).send(
            MailSendRequest(
                sender="agent://alpha",
                sender_did=alpha.identity.did,
                to=("agent://beta",),
                subject="Q3",
                body="What was Q3 revenue?",
                idempotency_key="ask-q3",
            )
        )

        assert await asyncio.wait_for(beta.turns.get(), timeout=10) == "Q3 was 4.2M."
        # beta's reply wakes alpha; alpha's answer would be a reply to a reply.
        assert await asyncio.wait_for(alpha.turns.get(), timeout=10) == "Thanks, and Q4?"
        await asyncio.sleep(0.5)

        assert await _bodies(fleet, _agent(alpha.identity), sent.thread_id) == [
            "What was Q3 revenue?",
            "Q3 was 4.2M.",
        ]
        assert beta.turns.empty()
    finally:
        await _stop(alpha, beta)


async def test_handoff_round_trip_wakes_the_recipient_by_signed_mail(
    fleet: _Fleet, tmp_path: Path
) -> None:
    operator = AgentIdentity.generate("arc", "operator")
    beta = _ScriptedAgent("beta", AgentIdentity.generate("arc", "executor"), ["", ""])
    await fleet.register(operator, "operator", EntityType.USER)
    await fleet.register(beta.identity, "beta", EntityType.AGENT)
    fleet.port.bind(fleet.mail(operator))
    await _start(beta, fleet, tmp_path / "beta")
    try:
        conversation = await _compose(fleet, operator, beta.identity, "Close", "Own the Q3 close.")
        await asyncio.wait_for(beta.turns.get(), timeout=10)

        handoff = await fleet.inbox.create_handoff(
            thread_id_for(beta.identity.did, conversation),
            sender=_human(operator),
            recipients=(_agent(beta.identity),),
            source_message_id=None,
            trace=TraceMetadata(),
            idempotency_key="handoff-1",
        )

        await asyncio.wait_for(beta.turns.get(), timeout=10)
        assert "Handoff: Close" in beta.prompts[-1]
        resolved = await fleet.inbox.resolve_handoff(
            handoff.handoff_id,
            recipient=_agent(beta.identity),
            actor_did=operator.did,
            status=HandoffStatus.ACCEPTED,
        )
        assert resolved.status is HandoffStatus.ACCEPTED
    finally:
        await _stop(beta)
