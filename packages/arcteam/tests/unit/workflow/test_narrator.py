"""COMP-010 — narration is one-way, carries no work, and wakes nobody (REQ-244)."""

from __future__ import annotations

from typing import Any

import pytest

from arcteam.types import MsgType
from arcteam.workflow.narrator import RunNarrator

from .conftest import RUNNER_DID, DroppingSender, RecordingSender

CHANNEL = "channel://onboarding"


async def narrate_everything(narrator: RunNarrator) -> None:
    await narrator.run_started(channel=CHANNEL, run_id="r1", workflow_id="wf", version=4)
    await narrator.node_started(channel=CHANNEL, run_id="r1", node_id="collect", owner="@sales")
    await narrator.node_completed(channel=CHANNEL, run_id="r1", node_id="collect")
    await narrator.handoff(channel=CHANNEL, run_id="r1", node_id="verify", owner="@ops")
    await narrator.gate_waiting(channel=CHANNEL, run_id="r1", node_id="manual_review")
    await narrator.gate_resolved(
        channel=CHANNEL, run_id="r1", node_id="manual_review", decision="approved", by="@josh"
    )
    await narrator.run_outcome(channel=CHANNEL, run_id="r1", status="done", detail="")


async def test_every_transition_is_narrated_to_the_bound_channel() -> None:
    sender = RecordingSender()
    await narrate_everything(RunNarrator(sender, sender_did=RUNNER_DID))

    assert len(sender.sent) == 7
    assert {tuple(m.to) for m in sender.sent} == {(CHANNEL,)}
    assert {m.sender for m in sender.sent} == {RUNNER_DID}


async def test_narration_never_wakes_an_agent() -> None:
    sender = RecordingSender()
    await narrate_everything(RunNarrator(sender, sender_did=RUNNER_DID))

    for message in sender.sent:
        assert message.msg_type is MsgType.INFO, "narration is never a task or a request"
        assert message.action_required is False
        assert message.mentions == [], "a mention fans out to an inbox and wakes an agent"
        assert message.meta["class"] == "narration"


async def test_narration_can_address_an_agent_for_gateway_relay() -> None:
    """A response binding may target an agent, not only a group channel — its
    gateway relays the narration out to Telegram/Slack. The old channel-only
    guard is gone; agent/user/role URIs are legal targets."""
    sender = RecordingSender()
    narrator = RunNarrator(sender, sender_did=RUNNER_DID)

    await narrator.run_outcome(channel="agent://sales", run_id="r1", status="done", detail="")
    assert [tuple(m.to) for m in sender.sent] == [("agent://sales",)]


async def test_a_dropped_send_never_reaches_the_caller() -> None:
    """Narration failure must not be able to fail a run."""
    sender = DroppingSender()
    await narrate_everything(RunNarrator(sender, sender_did=RUNNER_DID))
    assert sender.attempts == 7


async def test_an_unbound_workflow_narrates_nowhere() -> None:
    sender = RecordingSender()
    narrator = RunNarrator(sender, sender_did=RUNNER_DID)
    await narrator.run_outcome(channel=None, run_id="r1", status="done", detail="")
    assert sender.sent == []


async def test_an_empty_string_binding_narrates_nowhere() -> None:
    """An empty binding is the common 'no narration' case — it must be treated
    exactly like None, never parsed as a URI (parse_uri('') is the run-breaking
    'Invalid URI' defect this closes)."""
    sender = RecordingSender()
    narrator = RunNarrator(sender, sender_did=RUNNER_DID)
    await narrator.run_outcome(channel="", run_id="r1", status="done", detail="")
    assert sender.sent == []


def test_assert_channel_binding_accepts_empty_and_any_messaging_uri() -> None:
    from arcteam.workflow.narrator import assert_channel_binding

    # Empty / unset never raises — no narration is the default.
    assert_channel_binding(None)
    assert_channel_binding("")
    # Group channel, agent, user, and role targets are all legal.
    for uri in ("channel://onboarding", "agent://sales", "user://josh", "role://ops"):
        assert_channel_binding(uri)


def test_assert_channel_binding_rejects_a_bare_name() -> None:
    """A bare name saved by an older UI (`workflow-onboarding`, no scheme) is a
    definition defect — caught once at run start with a clear message, not a
    per-message surprise."""
    from arcteam.workflow.narrator import assert_channel_binding

    with pytest.raises(ValueError, match="Invalid URI"):
        assert_channel_binding("workflow-onboarding")


async def test_no_sender_is_a_working_configuration() -> None:
    narrator = RunNarrator(None, sender_did=RUNNER_DID)
    await narrate_everything(narrator)


def test_the_narrator_cannot_receive(monkeypatch: Any) -> None:
    assert not hasattr(RunNarrator, "poll")
    assert not hasattr(RunNarrator, "receive")
    assert not hasattr(RunNarrator, "subscribe")
