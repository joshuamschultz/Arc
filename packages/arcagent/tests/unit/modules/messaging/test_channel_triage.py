"""SPEC-055 — cheap channel-broadcast relevance triage.

Delivery to channel members already works (the inbox loop subscribes to member
channel streams). The problem is the OPPOSITE of "nobody answers": a channel
broadcast (no @mentions) wakes EVERY member's full run. This gate makes each
member run ONE cheap yes/no classification first, so only agents whose role the
message concerns pay for a full turn. @mentions and critical always bypass;
fail-open on any triage error.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arctrust import AgentIdentity

from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import _handle_incoming, _is_channel_broadcast
from tests.unit.modules.messaging.conftest import make_config_dict, make_operator_signer


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
) -> Any:
    from unittest.mock import MagicMock

    m = MagicMock()
    m.priority = priority
    m.action_required = False
    m.mentions = mentions or []
    m.to = to if to is not None else ["channel://personal"]
    m.sender = "agent://peer"
    m.signer_did = "did:arc:local:peer/aaaa"
    m.seq = 1
    m.body = body
    m.msg_type = "info"
    return m


class TestIsChannelBroadcast:
    def test_channel_no_mentions_is_broadcast(self) -> None:
        assert _is_channel_broadcast(_msg(to=["channel://ops"], mentions=[])) is True

    def test_channel_with_mentions_is_not_broadcast(self) -> None:
        assert _is_channel_broadcast(_msg(to=["channel://ops"], mentions=["did:x"])) is False

    def test_dm_is_not_broadcast(self) -> None:
        assert _is_channel_broadcast(_msg(to=["agent://me"], mentions=[])) is False

    def test_critical_is_not_gated(self) -> None:
        assert _is_channel_broadcast(_msg(to=["channel://ops"], priority="critical")) is False


async def _configure(tmp_path: Path, ident: AgentIdentity, *, triage: bool = True) -> Any:
    _runtime.configure(
        config=make_config_dict(entity_id="agent://me", channel_triage=triage),
        workspace=tmp_path,
        identity=ident,
        operator_signer=make_operator_signer(),
    )
    return _runtime.state()


class TestChannelTriageGate:
    @pytest.mark.asyncio
    async def test_irrelevant_broadcast_skips_run(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock()
        st.classify_fn = AsyncMock(return_value="NO")

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.classify_fn.assert_awaited_once()
        st.deliver_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_relevant_broadcast_runs(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.classify_fn = AsyncMock(return_value="YES")

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.classify_fn.assert_awaited_once()
        st.deliver_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_triage_error_fails_open_and_runs(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.classify_fn = AsyncMock(side_effect=RuntimeError("model down"))

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.deliver_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_no_classify_fn_runs(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.classify_fn = None

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.deliver_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_triage_disabled_runs_without_classify(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident, triage=False)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.classify_fn = AsyncMock(return_value="NO")

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[]))

        st.classify_fn.assert_not_called()
        st.deliver_fn.assert_called_once()

    @pytest.mark.asyncio
    async def test_mention_bypasses_triage(self, tmp_path: Path) -> None:
        ident = _identity()
        st = await _configure(tmp_path, ident)
        st.deliver_fn = AsyncMock(return_value="followed_up")
        st.classify_fn = AsyncMock(return_value="NO")

        await _handle_incoming(_msg(to=["channel://personal"], mentions=[ident.did]))

        st.classify_fn.assert_not_called()
        st.deliver_fn.assert_called_once()
