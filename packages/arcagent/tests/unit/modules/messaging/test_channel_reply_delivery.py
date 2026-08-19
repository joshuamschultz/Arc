"""A woken channel turn's answer must land back in the channel it came from.

An operator's group post in the arcui dashboard reaches a member via the inbox,
opens a turn, and the turn's final assistant text IS the reply — the same
contract every gateway turn already relies on. Unlike a gateway turn, nothing
streams that text back to the arcteam channel: the run shows a full answer and
the channel shows silence (the reported bug). The ``agent:post_respond`` hook
here closes that gap by posting the final text back onto the team bus.
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

from arcagent.core import turn_context
from arcagent.core.module_bus import EventContext
from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import (
    _format_delivery,
    deliver_channel_reply,
)


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    turn_context.set_inbound_channel(None)
    yield
    _runtime.reset()
    turn_context.set_inbound_channel(None)


def _configure(tmp_path: Path) -> Any:
    _runtime.configure(
        config=make_config_dict(entity_id="agent://me", entity_name="me"),
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        operator_signer=make_operator_signer(),
    )
    st = _runtime.state()
    st.svc = MagicMock()
    st.svc.send = AsyncMock()
    return st


def _ctx(final_text: str) -> EventContext:
    return EventContext(
        event="agent:post_respond",
        data={
            "result": None,
            "messages": [
                {"role": "user", "content": "what is the tech stack for NNL?"},
                {"role": "assistant", "content": final_text},
            ],
            "session_id": "s1",
            "automated": True,
        },
        agent_did="did:arc:local:agent/me",
        trace_id="t1",
    )


class TestChannelReplyDelivery:
    async def test_channel_origin_posts_final_text_to_that_channel(self, tmp_path: Path) -> None:
        st = _configure(tmp_path)
        turn_context.set_inbound_channel("channel://work")

        await deliver_channel_reply(_ctx("The NNL stack is Haystack + Qdrant + vLLM."))

        st.svc.send.assert_awaited_once()
        sent = st.svc.send.await_args.args[0]
        assert sent.to == ["channel://work"]
        assert sent.body == "The NNL stack is Haystack + Qdrant + vLLM."

    async def test_gateway_origin_is_not_posted_to_the_team_bus(self, tmp_path: Path) -> None:
        """A gateway turn (``platform:chat_id``) was already streamed by the
        executor — the hook must not post a duplicate onto the team bus."""
        st = _configure(tmp_path)
        turn_context.set_inbound_channel("web:room-123")

        await deliver_channel_reply(_ctx("answer"))

        st.svc.send.assert_not_called()

    async def test_originless_turn_is_not_posted(self, tmp_path: Path) -> None:
        st = _configure(tmp_path)
        turn_context.set_inbound_channel(None)

        await deliver_channel_reply(_ctx("answer"))

        st.svc.send.assert_not_called()

    async def test_empty_final_text_is_not_posted(self, tmp_path: Path) -> None:
        """The model answered via a tool and left no closing text — nothing to
        echo, and posting whitespace would be noise."""
        st = _configure(tmp_path)
        turn_context.set_inbound_channel("channel://work")

        await deliver_channel_reply(_ctx("   "))

        st.svc.send.assert_not_called()


class TestDeliveryPromptSteersTheReply:
    def test_channel_post_tells_the_model_its_answer_auto_posts(self) -> None:
        msg = MagicMock()
        msg.sender = "operator"
        msg.body = "what is the tech stack for NNL?"
        msg.msg_type = "info"
        msg.priority = "normal"
        msg.action_required = False
        msg.to = ["channel://work"]

        prompt = _format_delivery(msg)

        assert "#work" in prompt
        # It must NOT instruct messaging_send as the way to answer here.
        assert "Reply with messaging_send" not in prompt

    def test_direct_message_still_asks_for_messaging_send(self) -> None:
        msg = MagicMock()
        msg.sender = "peer"
        msg.body = "can you review this?"
        msg.msg_type = "info"
        msg.priority = "normal"
        msg.action_required = False
        msg.to = ["agent://me"]

        prompt = _format_delivery(msg)

        assert "messaging_send" in prompt
