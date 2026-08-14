"""Journey: a message from Telegram reaches the agent and the answer goes back.

A remote channel is a different entry point to the same agent, and every part
between the two is separate code: the platform adapter's authorization gate, the
session router, the executor's delivery hand-off, and the adapter's outbound
send. The repo already drives a real adapter end to end for *pairing*, but that
test stops at a recording stub — the agent half, where this week's outage lived,
was never on the path.

Real here: ``TelegramAdapter._handle_update``, ``SessionRouter``,
``AsyncioExecutor``, and the started ``ArcAgent``. Faked: the Telegram HTTP
client (there is no bot to talk to) and the LLM wire.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from arcgateway.adapters.telegram.adapter import TelegramAdapter
from arcgateway.executor import AsyncioExecutor
from arcgateway.session import SessionRouter

from .conftest import ScriptedLLM

TELEGRAM_USER_ID = 4242
TELEGRAM_CHAT_ID = 5555


def _update(text: str, *, user_id: int = TELEGRAM_USER_ID, update_id: int = 1) -> MagicMock:
    """A python-telegram-bot Update carrying one text message."""
    update = MagicMock()
    update.update_id = update_id
    update.effective_user = MagicMock()
    update.effective_user.id = user_id
    update.effective_chat = MagicMock()
    update.effective_chat.id = TELEGRAM_CHAT_ID
    # A text-only message. Every attachment slot is pinned to None because a
    # bare MagicMock invents any attribute asked of it, so the adapter would
    # otherwise find five phantom photos and documents on a message that is
    # just words.
    update.effective_message = MagicMock(
        text=text, photo=None, document=None, audio=None, voice=None, video=None
    )
    return update


async def _deliver(adapter: TelegramAdapter, router: SessionRouter, update: MagicMock) -> None:
    """Hand an update to the adapter and wait for the turn it spawns.

    ``SessionRouter.handle`` gives every accepted message its own handoff task
    and returns immediately, so an assertion made right after ``_handle_update``
    races the agent and reads an empty outbox. Awaiting the spawned tasks is
    what makes the outbound half observable at all.
    """
    await adapter._handle_update(update, context=MagicMock())
    while router._pending_tasks:
        await asyncio.gather(*tuple(router._pending_tasks), return_exceptions=True)


@pytest.fixture
def telegram(agent: Any) -> tuple[TelegramAdapter, MagicMock, SessionRouter]:
    """A real adapter wired to a real executor over the real started agent."""

    async def _agent_factory(_agent_did: str) -> Any:
        return agent

    router = SessionRouter(executor=AsyncioExecutor(agent_factory=_agent_factory))
    adapter = TelegramAdapter(
        bot_token="journey-token",
        allowed_user_ids=[TELEGRAM_USER_ID],
        on_message=router.handle,
        agent_did=agent._identity.did,
        require_pairing=False,
    )
    adapter._bot_id = 999
    router.register_adapter(adapter)

    application = MagicMock()
    application.bot.send_message = AsyncMock()
    adapter._application = application
    return adapter, application, router


def _sent_texts(application: MagicMock) -> list[str]:
    """Every outbound message body, in order."""
    return [
        call.kwargs.get("text", "")
        for call in application.bot.send_message.call_args_list
        if call.kwargs.get("text")
    ]


async def test_a_telegram_message_gets_the_agent_reply_back(
    telegram: tuple[TelegramAdapter, MagicMock, SessionRouter], scripted_llm: ScriptedLLM
) -> None:
    """Someone messages the bot; the agent's answer is delivered to that chat."""
    adapter, application, router = telegram
    scripted_llm.replies.append("Shipping on Thursday.")

    await _deliver(adapter, router, _update("when does it ship?"))

    assert "Shipping on Thursday." in " ".join(_sent_texts(application))
    assert "when does it ship?" in scripted_llm.last_prompt_text


async def test_the_reply_goes_to_the_chat_it_came_from(
    telegram: tuple[TelegramAdapter, MagicMock, SessionRouter], scripted_llm: ScriptedLLM
) -> None:
    """The answer must return to the originating chat, not a default target.

    Origin threading has broken here before: a message posted in one surface was
    answered on another because the reply fell back to whatever channel was most
    recent. Asserting only that *a* send happened would not have caught it.
    """
    adapter, application, router = telegram

    await _deliver(adapter, router, _update("hello"))

    assert application.bot.send_message.call_args_list, "nothing was sent back"
    targets = {call.kwargs.get("chat_id") for call in application.bot.send_message.call_args_list}
    assert targets == {TELEGRAM_CHAT_ID}, f"reply went to {targets}, not the origin chat"


async def test_an_unauthorized_user_never_reaches_the_agent(
    telegram: tuple[TelegramAdapter, MagicMock, SessionRouter], scripted_llm: ScriptedLLM
) -> None:
    """The allowlist gate is the bot's front door — a stranger must not get a turn.

    Without this, the reply journeys above would pass just as well with the
    authorization check deleted, and a public bot token would hand any stranger
    on Telegram a live agent with the operator's tools.
    """
    adapter, _application, router = telegram

    await _deliver(adapter, router, _update("let me in", user_id=999_999))

    assert scripted_llm.calls == [], "an unauthorized user reached the model"
