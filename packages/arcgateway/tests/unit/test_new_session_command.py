"""Unit test for /new — it rotates the caller's session via the router."""

from __future__ import annotations

from typing import Any, cast

import pytest

from arcgateway.commands.base import CommandContext
from arcgateway.commands.new_session import NewSessionCommand
from arcgateway.executor import InboundEvent
from arcgateway.session import SessionRouter, build_session_key


class _NullExecutor:
    async def run(self, event: InboundEvent) -> Any:  # pragma: no cover - unused
        raise AssertionError("executor must not run for a command")


@pytest.mark.asyncio
async def test_new_session_command_rotates_and_confirms() -> None:
    router = SessionRouter(executor=cast(Any, _NullExecutor()))
    agent, user = "did:arc:agent:bot", "did:arc:user:alice"
    before = router.current_session_key(agent, user)
    assert before == build_session_key(agent, user)  # generation 0 = plain key

    event = InboundEvent(
        platform="web", chat_id="c1", user_did=user, agent_did=agent,
        session_key=before, message="/new",
    )
    ctx = CommandContext(event=event, agent_did=agent, user_did=user, args="", router=router)

    reply = await NewSessionCommand().handle(ctx)

    assert reply is not None and "fresh session" in reply.lower()
    after = router.current_session_key(agent, user)
    assert after != before  # rotated to a new, empty session key
