"""SPEC-055 + SPEC-068 — the channel relevance gate, at the delivery boundary.

A channel broadcast (no @mentions) reaches every member, and the gate makes each
one run a single cheap yes/no classification before paying for a full turn.
@mentions and critical always bypass it.

**This file previously pinned the gate as fail-OPEN**, in
``test_triage_error_fails_open_and_runs`` and ``test_no_oneshot_fn_runs``. That
was reversed deliberately (SPEC-068 D1a): a cost control whose failure mode is
to spend the maximum is the wrong shape, and fail-open meant a broken or slow
model bought a full agentic turn in *every* member of the channel on *every*
message. The two tests are kept, inverted, and named for the new invariant so
nobody restores the old one by accident. An @mention is the deterministic
override that makes fail-closed safe: it bypasses the gate entirely.

The other change visible here: a broadcast's sender must be a registered human
for the fan-out to happen at all (D4a), so these fixtures register one. An
agent's own un-addressed post wakes nobody, which is what makes A -> B -> A
impossible rather than merely bounded.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

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
    body: str = "hello team",
    sender_did: str = HUMAN_DID,
) -> Any:
    from unittest.mock import MagicMock

    m = MagicMock()
    m.priority = priority
    m.action_required = False
    m.mentions = mentions or []
    m.to = to if to is not None else ["channel://personal"]
    m.sender = sender_did
    m.signer_did = sender_did
    m.seq = 1
    m.hop = 0
    m.id = "msg_1"
    m.body = body
    m.msg_type = "info"
    return m


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


async def _configure(tmp_path: Path, ident: AgentIdentity, *, triage: bool = True) -> Any:
    from arcteam.types import Entity, EntityType

    _runtime.configure(
        config=make_config_dict(entity_id="agent://me", channel_triage=triage),
        workspace=tmp_path,
        identity=ident,
        operator_signer=make_operator_signer(),
    )
    st = _runtime.state()
    # The fan-out only happens for a human's post, so the sender has to exist as
    # one. Without this the gate is never even reached (D4a).
    await st.registry.register(
        Entity(
            did=HUMAN_DID,
            handle="operator",
            id="user://operator",
            name="Operator",
            type=EntityType.USER,
        )
    )
    return st


class TestChannelTriageGate:
    @pytest.mark.asyncio
    async def test_irrelevant_broadcast_skips_run(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock()
        st.oneshot_fn = AsyncMock(return_value="NO")

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.oneshot_fn.assert_awaited_once()
        st.deliver_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_relevant_broadcast_runs(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(return_value="YES")

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.oneshot_fn.assert_awaited_once()
        st.deliver_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_gate_error_fails_closed_and_stays_silent(self, tmp_path: Path) -> None:
        """Inverted from fail-open (SPEC-068 D1a). Silence is the cheap failure."""
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(side_effect=RuntimeError("model down"))

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.deliver_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_oneshot_fn_stays_silent(self, tmp_path: Path) -> None:
        """Inverted from fail-open. An unwired gate must not authorise the spend."""
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = None

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.deliver_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_triage_disabled_runs_without_classify(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident, triage=False)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(return_value="NO")

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.oneshot_fn.assert_not_called()
        st.deliver_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_mention_bypasses_triage(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(return_value="NO")

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[ident.did]))

        st.oneshot_fn.assert_not_called()
        st.deliver_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_agent_broadcast_never_reaches_the_gate(self, tmp_path: Path) -> None:
        """The loop guard (D4a): an agent's reply is itself an un-addressed post."""
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.oneshot_fn = AsyncMock(return_value="YES")

        await _handle_incoming(_msg(sender_did="did:arc:local:agent/peer", mentions=[]))

        st.oneshot_fn.assert_not_called()
        st.deliver_fn.assert_not_called()
