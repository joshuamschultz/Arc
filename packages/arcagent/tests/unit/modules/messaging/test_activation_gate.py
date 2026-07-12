"""SPEC-055 — mention-scoped inbox activation (relevance triage).

Every channel member currently spins a full LLM run on every pushed message,
even one that @mentions someone else — Anthropic's ~15x multi-agent token
anti-pattern. ``_should_activate(msg, identity)`` decides whether *this*
agent's run should wake at all, before ``_handle_incoming`` reaches
``deliver_fn``/``agent_run_fn``:

  * ``priority == critical``            -> always activates (kill-switch traffic).
  * ``not msg.mentions`` (DM/broadcast) -> always activates (only recipient, or
    nobody was singled out).
  * ``identity.did in msg.mentions``    -> activates (I was addressed).
  * otherwise                           -> ack-and-ignore, no run, no follow_up.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arctrust import AgentIdentity

from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import _handle_incoming
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


class TestShouldActivate:
    """Truth table for the activation predicate (R1-R4)."""

    def test_mentioned_agent_activates(self) -> None:
        from arcagent.modules.messaging.capabilities import _should_activate

        ident = _identity()
        msg = _msg(mentions=[ident.did])
        assert _should_activate(msg, ident) is True

    def test_non_mentioned_agent_does_not_activate(self) -> None:
        from arcagent.modules.messaging.capabilities import _should_activate

        ident = _identity()
        other = _identity()
        msg = _msg(mentions=[other.did])
        assert _should_activate(msg, ident) is False

    def test_empty_mentions_activates_broadcast_or_dm(self) -> None:
        from arcagent.modules.messaging.capabilities import _should_activate

        ident = _identity()
        msg = _msg(mentions=[])
        assert _should_activate(msg, ident) is True

    def test_critical_overrides_non_mention(self) -> None:
        from arcagent.modules.messaging.capabilities import _should_activate

        ident = _identity()
        other = _identity()
        msg = _msg(priority="critical", mentions=[other.did])
        assert _should_activate(msg, ident) is True


class TestHandleIncomingGating:
    """``_handle_incoming`` must consult the gate before touching the run."""

    @pytest.mark.asyncio
    async def test_non_activating_message_runs_nothing(self, tmp_path: Path) -> None:
        """A channel msg mentioning someone else wakes no run and doesn't raise."""
        ident = _identity()
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me"),
            workspace=tmp_path,
            identity=ident,
            operator_signer=make_operator_signer(),
        )
        st = _runtime.state()
        deliver_fn = AsyncMock()
        run_fn = AsyncMock()
        st.deliver_fn = deliver_fn
        st.agent_run_fn = run_fn

        other = _identity()
        await _handle_incoming(_msg(mentions=[other.did]))

        deliver_fn.assert_not_called()
        run_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_activating_message_still_delivers(self, tmp_path: Path) -> None:
        """A message that passes the gate still runs the existing delivery path."""
        ident = _identity()
        _runtime.configure(
            config=make_config_dict(entity_id="agent://me"),
            workspace=tmp_path,
            identity=ident,
            operator_signer=make_operator_signer(),
        )
        st = _runtime.state()
        deliver_fn = AsyncMock(return_value="followed_up")
        st.deliver_fn = deliver_fn

        await _handle_incoming(_msg(mentions=[ident.did]))

        deliver_fn.assert_called_once()
