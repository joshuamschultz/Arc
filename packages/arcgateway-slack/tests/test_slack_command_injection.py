"""Slash-command intake for Slack.

Slack delivers slash commands out-of-band from messages, so the adapter must
re-inject them as an InboundEvent whose ``message`` is ``/<name> <text>`` —
letting the gateway's single command interceptor handle them exactly like
Telegram/web (which deliver ``/cmd`` as message text).
"""

from __future__ import annotations

import pytest
from arcgateway.executor import InboundEvent

from arcgateway_slack.adapter import SlackAdapter


def _make_adapter() -> tuple[SlackAdapter, list[InboundEvent]]:
    received: list[InboundEvent] = []

    async def _on_message(event: InboundEvent) -> None:
        received.append(event)

    adapter = SlackAdapter(
        bot_token="xoxb-valid-token",
        app_token="xapp-valid-token",
        allowed_user_ids=["U123"],
        on_message=_on_message,
        agent_did="did:arc:agent:bot",
        dedup_db_path=None,
    )
    return adapter, received


def test_set_command_names_stores_tuple() -> None:
    adapter, _ = _make_adapter()
    adapter.set_command_names(["new", "help"])
    assert adapter._command_names == ("new", "help")


@pytest.mark.asyncio
async def test_slash_command_reinjected_as_message_event() -> None:
    adapter, received = _make_adapter()

    await adapter._handle_slash_command(
        {"command": "/new", "text": "", "user_id": "U123", "channel_id": "C1"}
    )

    assert len(received) == 1
    event = received[0]
    assert event.platform == "slack"
    assert event.message == "/new"  # the command interceptor sees a normal "/cmd"
    assert event.user_did == "slack:U123"
    assert event.chat_id == "C1"
    assert event.agent_did == "did:arc:agent:bot"


@pytest.mark.asyncio
async def test_slash_command_preserves_args() -> None:
    adapter, received = _make_adapter()

    await adapter._handle_slash_command(
        {"command": "/echo", "text": "hello world", "user_id": "U123", "channel_id": "C1"}
    )

    assert received[0].message == "/echo hello world"
