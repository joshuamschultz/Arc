"""ADR-032 — responder selection at the delivery boundary, over real services.

The ladder is unit-tested against fakes elsewhere. This file runs it through the
module's actual runtime: a real ``EntityRegistry``, a real ``MessagingService``,
a real ``DigestStore``, and the real ``_handle_incoming`` an inbox delivery goes
through. It is the test that would have failed on the reported bug, because a
digest published at ingest is what makes the right agent findable, and nothing
in the fake world proves the wiring reaches it.

The gate this replaces is gone. Where that file pinned "fail closed on a broken
classifier", the invariant now is stronger and simpler: **the message is
answered.** A missing or broken router falls back to a deterministic ranking,
and a ranking that finds nothing falls back to a named responder.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.activation import is_overheard
from arcagent.modules.messaging.capabilities import _handle_incoming

HUMAN_DID = "did:arc:local:user/operator"
PEER_DID = "did:arc:local:agent/peer"
CHANNEL = "work"


@pytest.fixture(autouse=True)
def _reset_runtime() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _identity() -> AgentIdentity:
    return AgentIdentity.generate(org="local", agent_type="agent")


def _msg(
    *,
    to: list[str] | None = None,
    mentions: list[str] | None = None,
    priority: str = "normal",
    body: str = "who has the technical requirements for NNL?",
    sender_did: str = HUMAN_DID,
) -> Any:
    m = MagicMock()
    m.priority = priority
    m.action_required = False
    m.mentions = mentions or []
    m.to = to if to is not None else [f"channel://{CHANNEL}"]
    m.sender = sender_did
    m.signer_did = sender_did
    m.seq = 1
    m.hop = 0
    m.id = "msg_1"
    m.body = body
    m.msg_type = "info"
    return m


async def _configure(
    tmp_path: Path,
    ident: AgentIdentity,
    *,
    holdings: list[str] | None = None,
    peer_holdings: list[str] | None = None,
    responder: str = "",
    **cfg: Any,
) -> Any:
    """A real two-agent room with a real operator, real channel and real digests."""
    from arcteam.digest import AgentDigest, DigestEntry
    from arcteam.types import Channel, Entity, EntityType

    _runtime.configure(
        config=make_config_dict(entity_id="agent://me", entity_name="me", **cfg),
        workspace=tmp_path,
        identity=ident,
        operator_signer=make_operator_signer(),
    )
    st = _runtime.state()
    # A broadcast only fans out for a human's post, so the sender has to exist
    # as one; without it nothing downstream is reached at all (SPEC-068 D4a).
    await st.registry.register(
        Entity(
            did=HUMAN_DID,
            handle="operator",
            id="user://operator",
            name="Operator",
            type=EntityType.USER,
        )
    )
    for did, handle in ((ident.did, "me"), (PEER_DID, "peer")):
        await st.registry.register(
            Entity(
                did=did,
                handle=handle,
                id=f"agent://{handle}",
                name=handle,
                type=EntityType.AGENT,
            )
        )
    await st.svc.create_channel(
        Channel(
            name=CHANNEL,
            members=[HUMAN_DID, ident.did, PEER_DID],
            responder=responder,
        )
    )
    for did, handle, titles in (
        (ident.did, "me", holdings),
        (PEER_DID, "peer", peer_holdings),
    ):
        if titles is None:
            continue
        await st.digests.publish(
            AgentDigest(
                agent_did=did,
                handle=handle,
                entries=[
                    DigestEntry(artifact_id=f"{handle}-{i}", title=title)
                    for i, title in enumerate(titles)
                ],
            )
        )
    return st


class TestTheReportedFailure:
    async def test_the_agent_that_published_the_pointer_is_delivered_to(
        self, tmp_path: Path
    ) -> None:
        """Nobody is named, and the message reaches the agent holding the document."""
        ident = _identity()
        st = await _configure(
            tmp_path,
            ident,
            holdings=["NNL technical requirements"],
            peer_holdings=["Q3 brand voice guidelines"],
        )
        st.deliver_fn = AsyncMock(return_value="followed_up")

        await _handle_incoming(_msg())

        st.deliver_fn.assert_called_once()

    async def test_the_agent_holding_something_else_is_not(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(
            tmp_path,
            ident,
            holdings=["Q3 brand voice guidelines"],
            peer_holdings=["NNL technical requirements"],
        )
        st.deliver_fn = AsyncMock(return_value="followed_up")

        await _handle_incoming(_msg())

        st.deliver_fn.assert_not_called()

    async def test_it_is_answered_without_a_model_call(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(
            tmp_path,
            ident,
            holdings=["NNL technical requirements"],
            peer_holdings=["Q3 brand voice guidelines"],
        )
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(return_value="me")

        await _handle_incoming(_msg())

        st.oneshot_fn.assert_not_called()
        st.deliver_fn.assert_called_once()

    async def test_the_decision_is_audited_with_its_reason(self, tmp_path: Path) -> None:
        """A wrong route must leave evidence — self-assessment never did."""
        ident = _identity()
        st = await _configure(tmp_path, ident, holdings=["NNL technical requirements"])
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.telemetry = MagicMock()

        await _handle_incoming(_msg())

        event, payload = st.telemetry.audit_event.call_args[0]
        assert event == "messaging.activation"
        assert payload["reason"] == "routed"


class TestNothingScores:
    async def test_the_named_responder_still_answers(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident, holdings=["logo usage rules"], responder=ident.did)
        st.deliver_fn = AsyncMock(return_value="followed_up")

        await _handle_incoming(_msg(body="zzzz qqqq"))

        st.deliver_fn.assert_called_once()

    async def test_an_agent_that_published_nothing_can_still_be_the_responder(
        self, tmp_path: Path
    ) -> None:
        """A fresh agent has an empty digest; that must not make the room silent."""
        ident = _identity()
        st = await _configure(tmp_path, ident, responder=ident.did)
        st.deliver_fn = AsyncMock(return_value="followed_up")

        await _handle_incoming(_msg())

        st.deliver_fn.assert_called_once()

    async def test_a_broken_router_does_not_produce_silence(self, tmp_path: Path) -> None:
        """Where the old gate failed closed into silence, the ranking now stands."""
        ident = _identity()
        st = await _configure(
            tmp_path, ident, holdings=["NNL technical requirements"], responder=ident.did
        )
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(side_effect=RuntimeError("model down"))

        await _handle_incoming(_msg())

        st.deliver_fn.assert_called_once()

    async def test_no_router_bound_does_not_produce_silence(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(
            tmp_path, ident, holdings=["NNL technical requirements"], responder=ident.did
        )
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = None

        await _handle_incoming(_msg())

        st.deliver_fn.assert_called_once()


class TestExplicitAddress:
    async def test_a_mention_beats_the_router(self, tmp_path: Path) -> None:
        """Addressed directly, with nothing published and nothing relevant held."""
        ident = _identity()
        st = await _configure(
            tmp_path,
            ident,
            holdings=["logo usage rules"],
            peer_holdings=["NNL technical requirements"],
        )
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(return_value="peer")

        await _handle_incoming(_msg(mentions=[ident.did]))

        st.oneshot_fn.assert_not_called()
        st.deliver_fn.assert_called_once()

    async def test_naming_a_teammate_silences_this_agent(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident, holdings=["NNL technical requirements"])
        st.deliver_fn = AsyncMock(return_value="followed_up")

        await _handle_incoming(_msg(mentions=[PEER_DID]))

        st.deliver_fn.assert_not_called()


class TestLoopGuard:
    async def test_an_agents_own_broadcast_wakes_nobody(self, tmp_path: Path) -> None:
        """A -> B -> A is impossible by construction, not merely bounded."""
        ident = _identity()
        st = await _configure(tmp_path, ident, holdings=["NNL technical requirements"])
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(return_value="me")

        await _handle_incoming(_msg(sender_did=PEER_DID))

        st.oneshot_fn.assert_not_called()
        st.deliver_fn.assert_not_called()

    async def test_routing_disabled_delivers_without_ranking(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident, channel_route=False)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(return_value="me")

        await _handle_incoming(_msg())

        st.oneshot_fn.assert_not_called()
        st.deliver_fn.assert_called_once()


class TestIsOverheard:
    """What may be answered but must never be remembered (``7812264f``)."""

    def test_channel_no_mentions_is_overheard(self) -> None:
        assert is_overheard(_msg(to=["channel://ops"], mentions=[])) is True

    def test_channel_with_mentions_is_not_overheard(self) -> None:
        assert is_overheard(_msg(to=["channel://ops"], mentions=["did:x"])) is False

    def test_dm_is_not_overheard(self) -> None:
        assert is_overheard(_msg(to=["agent://me"], mentions=[])) is False

    def test_critical_is_not_overheard(self) -> None:
        assert is_overheard(_msg(to=["channel://ops"], priority="critical")) is False
