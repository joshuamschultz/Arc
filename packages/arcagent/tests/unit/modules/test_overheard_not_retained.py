"""An agent may answer what it overhears; it must not remember it.

Posting to a shared channel without @mentioning anyone wakes EVERY member agent,
and each one captures the post into its own durable memory. Answering is the
design. Retaining is not: one operator's message to the sales agent ends up in the
trader's memory, the marketer's, and every other member's — permanently, and in
each of their future prompts.

The operator's requirement is that knowledge, files and sessions stay contained to
the agent they belong to. Triage cannot deliver that, because triage answers a
different question: "is replying my job?", never "is this mine to keep?" — and it
is fail-open by design, so an agent that stays silent has usually still stored it.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arcagent.core import turn_context
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.capabilities import capture_user


class _Ctx:
    def __init__(self, task: str) -> None:
        self.data: dict[str, Any] = {"task": task}


class _RecordingBrain:
    """Captures what memory was asked to retain."""

    def __init__(self) -> None:
        self.captured: list[str] = []

    async def capture(self, text: str, *, kind: str = "observation", **_: Any) -> None:
        self.captured.append(text)

    async def authorize(self, operation: str, *, caller_did: str = "") -> bool:
        return True


@pytest.fixture(autouse=True)
def _clean() -> Any:
    _runtime.reset()
    turn_context.set_overheard(False)
    yield
    turn_context.set_overheard(False)
    _runtime.reset()


def _bind(tmp_path: Any) -> _RecordingBrain:
    _runtime.configure(
        config={"brain": "none"}, workspace=tmp_path, agent_did="did:arc:test:listener"
    )
    state = _runtime.state()
    brain = _RecordingBrain()
    state.brain = brain
    state.active = True
    return brain


async def test_an_overheard_broadcast_is_not_retained(tmp_path: Any) -> None:
    """The whole point: a post addressed to nobody must not enter this agent's memory."""
    brain = _bind(tmp_path)
    turn_context.set_overheard(True)

    await capture_user(_Ctx("transcript with a possible sales guy for CTG Federal"))

    assert brain.captured == [], "an overheard broadcast entered durable memory"


async def test_a_message_addressed_to_this_agent_is_retained(tmp_path: Any) -> None:
    """The paired positive — without it, "retain nothing" would pass this suite."""
    brain = _bind(tmp_path)
    turn_context.set_overheard(False)

    await capture_user(_Ctx("please quote this customer"))

    assert brain.captured == ["please quote this customer"]


async def test_the_flag_does_not_leak_into_the_next_turn(tmp_path: Any) -> None:
    """A turn that overheard must not make the following turn forgetful.

    The flag is ambient per-turn state, so a dispatch that failed to reset it would
    silently stop an agent remembering anything at all — a far worse failure than
    the one being fixed, and a silent one.
    """
    brain = _bind(tmp_path)
    turn_context.set_overheard(True)
    await capture_user(_Ctx("overheard chatter"))

    turn_context.set_overheard(False)
    await capture_user(_Ctx("a direct request"))

    assert brain.captured == ["a direct request"]


# ---------------------------------------------------------------------------
# The flag has to be SET by the real inbox path, not just honoured
# ---------------------------------------------------------------------------


class _Msg:
    """One inbound arcteam message."""

    def __init__(self, *, to: list[str], mentions: list[str] | None = None) -> None:
        self.to = to
        self.mentions = mentions or []
        self.priority = "normal"
        self.sender = "did:arc:test:operator"
        self.signer_did = "did:arc:test:operator"
        self.body = "transcript with a possible sales guy for CTG Federal"
        self.msg_type = "chat"
        self.action_required = False
        self.id = "m1"
        self.seq = 1
        self.hop = 0


class _HumanRegistry:
    """Resolves one DID to a ``user`` entity — the sender of these fixtures.

    An un-addressed channel post only fans out when a registered human wrote it
    (SPEC-068 D4a), so the sender has to exist as one for these to reach the
    delivery call at all.
    """

    def __init__(self, did: str) -> None:
        self._did = did

    async def list_entities(self) -> list[Any]:
        from arcteam.types import EntityType

        return [SimpleNamespace(did=self._did, type=EntityType.USER)]


class _MsgState:
    """The fields ``_handle_incoming`` reads — enough to exercise its dispatch.

    Built directly rather than through ``messaging._runtime.configure``, which
    bootstraps arcteam and demands a real operator signer: that is the module's
    startup contract, not the routing decision under test here.
    """

    def __init__(self, did: str) -> None:
        self.identity = SimpleNamespace(did=did)
        self.processing_lock = asyncio.Lock()
        self.deliver_fn: Any = None
        self.agent_run_fn: Any = None
        self.oneshot_fn = None
        self.telemetry = None
        self.config = SimpleNamespace(
            channel_route=False,
            entity_name="listener",
            entity_role="listener",
            channel_cooldown_seconds=0.0,
            channel_answer_cap=0,
            route_timeout_seconds=5.0,
            route_failure_threshold=5,
            route_base_wait_seconds=30.0,
        )
        self.agent_name = "listener"
        self.channel_last_woken: dict[str, float] = {}
        self.channel_breakers: dict[str, Any] = {}
        self.registry = _HumanRegistry("did:arc:test:operator")
        self.svc = None
        self.digests = None


async def _deliver_through_inbox(monkeypatch: Any, message: Any) -> list[dict[str, Any]]:
    """Run one message through the real inbox path; return the delivery calls."""
    from arcagent.modules.messaging import capabilities as messaging

    delivered: list[dict[str, Any]] = []
    state = _MsgState("did:arc:test:listener")

    async def _deliver(**kwargs: Any) -> None:
        delivered.append(kwargs)

    state.deliver_fn = _deliver
    monkeypatch.setattr(messaging._runtime, "state", lambda: state)
    await messaging._handle_incoming(message)
    return delivered


async def test_an_unaddressed_channel_post_is_delivered_as_overheard(
    monkeypatch: Any,
) -> None:
    """The wiring, end to end: the inbox must MARK a broadcast, not merely allow it.

    A flag nothing sets is the exact shape of bug this repo keeps finding — a correct
    predicate with dead activating wiring — so the delivery call itself is asserted,
    not the helper that computes the flag.
    """
    delivered = await _deliver_through_inbox(monkeypatch, _Msg(to=["channel://team"]))

    assert delivered, "an un-addressed channel post never reached the agent"
    assert delivered[0]["overheard"] is True


async def test_an_at_mention_is_not_overheard(monkeypatch: Any) -> None:
    """Addressed to this agent is its own business — it must be remembered."""
    delivered = await _deliver_through_inbox(
        monkeypatch, _Msg(to=["channel://team"], mentions=["did:arc:test:listener"])
    )

    assert delivered, "an @mention for this agent was dropped"
    assert delivered[0]["overheard"] is False


async def test_a_direct_message_is_not_overheard(monkeypatch: Any) -> None:
    """A DM names one recipient by construction — never a broadcast."""
    delivered = await _deliver_through_inbox(monkeypatch, _Msg(to=["agent://listener"]))

    assert delivered
    assert delivered[0]["overheard"] is False
