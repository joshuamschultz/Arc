"""SPEC-055 — ``_handle_incoming`` must consult the gate before touching the run.

Every channel member would otherwise spin a full LLM run on every pushed
message, even one that @mentions someone else — the ~15x multi-agent token
anti-pattern.

This file keeps the **wiring** assertion: the gate is actually consulted on the
real inbox path. The gate's own truth table moved to
``test_activation_ladder.py`` when the predicate became the full ladder
(SPEC-068), and every case that used to live here is asserted there:

  * mentioned            -> ``test_mention_bypasses_the_gate_entirely``
  * names another agent  -> ``test_message_naming_other_agents_does_not_wake_me``
  * DM / no mentions     -> ``test_direct_message_wakes_without_a_gate``
  * critical             -> ``test_critical_always_wakes``

Keeping a copy here would be a second truth table to drift against the first.
The wiring test is the one that cannot be inferred from the predicate, and is
the shape of bug this repo keeps finding: a correct predicate nothing calls.
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
from arcagent.modules.messaging.capabilities import _handle_incoming


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
