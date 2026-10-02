"""Gate approval cards get inline buttons; a press is the typed ``/gate`` line (alpha-2 #67).

The button carries no authority of its own. Its callback data is the command
line, matched against the ``/gate`` grammar, and re-entered as a message from
the user who pressed it — so it meets the same admission, pairing, command and
gate authorization a typed command meets.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcgateway.adapters.base import InboundDraft
from arcgateway.adapters.telegram.adapter import TelegramAdapter
from arcgateway.delivery import DeliveryTarget
from arcgateway.parts import flatten_text

GATE = "wf/run-0123456789ab/review/0"
CARD = (
    "Workflow gated (run run-0123456789ab) is waiting on gate review.\n"
    f"/gate {GATE} approve\n/gate {GATE} reject\n/gate {GATE} revise"
)


def _adapter(on_message: Any = None, allowed: list[int] | None = None) -> TelegramAdapter:
    adapter = TelegramAdapter(
        bot_token="test-token-abc123",
        allowed_user_ids=allowed if allowed is not None else [42],
        on_message=on_message or AsyncMock(),
        agent_did="did:arc:agent:test",
        require_pairing=False,
    )
    app = MagicMock()
    app.bot.send_message = AsyncMock()
    adapter._application = app
    adapter._bot_id = 999
    return adapter


def _press(user_id: int, data: Any) -> MagicMock:
    update = MagicMock()
    update.update_id = 7
    update.callback_query.from_user.id = user_id
    update.callback_query.from_user.first_name = "Josh"
    update.callback_query.from_user.username = "josh"
    update.callback_query.message.chat.id = 1000
    update.callback_query.data = data
    update.callback_query.answer = AsyncMock()
    return update


@pytest.mark.asyncio
async def test_a_gate_card_is_sent_with_one_button_per_verb() -> None:
    adapter = _adapter()

    await adapter.send(DeliveryTarget.parse("telegram:12345"), CARD)

    kwargs = adapter._application.bot.send_message.call_args.kwargs
    assert kwargs["text"] == CARD
    [row] = kwargs["reply_markup"].inline_keyboard
    assert [b.text for b in row] == ["Approve", "Reject", "Revise"]
    assert [b.callback_data for b in row] == [
        f"/gate {GATE} approve",
        f"/gate {GATE} reject",
        f"/gate {GATE} revise",
    ]


@pytest.mark.asyncio
async def test_an_ordinary_message_gets_no_buttons() -> None:
    adapter = _adapter()

    await adapter.send(DeliveryTarget.parse("telegram:12345"), "/gate is how you decide")

    assert "reply_markup" not in adapter._application.bot.send_message.call_args.kwargs


@pytest.mark.asyncio
async def test_a_gate_line_too_long_for_callback_data_gets_no_button() -> None:
    adapter = _adapter()
    long_gate = "wf/run-0123456789ab/" + "n" * 60 + "/0"

    await adapter.send(DeliveryTarget.parse("telegram:12345"), f"/gate {long_gate} approve")

    assert "reply_markup" not in adapter._application.bot.send_message.call_args.kwargs


@pytest.mark.asyncio
async def test_a_press_is_the_typed_command_from_the_presser() -> None:
    on_message = AsyncMock()
    adapter = _adapter(on_message)

    await adapter._handle_callback(_press(42, f"/gate {GATE} approve"), context=MagicMock())

    [event] = [c.args[0] for c in on_message.await_args_list]
    assert isinstance(event, InboundDraft)
    assert flatten_text(event.parts) == f"/gate {GATE} approve"
    assert event.user_did == "did:arc:telegram:42"
    assert event.chat_id == "1000"


@pytest.mark.parametrize(
    "data",
    [
        "/new",
        f"/gate {GATE} approve\n/new",
        f"/gate {GATE} approve role:reviewer",
        f"/gate {GATE} delete",
        "please approve",
        None,
    ],
)
@pytest.mark.asyncio
async def test_forged_callback_data_never_becomes_a_message(data: Any) -> None:
    on_message = AsyncMock()
    adapter = _adapter(on_message)
    audited: list[str] = []
    adapter._audit = lambda name, payload: audited.append(name)  # type: ignore[method-assign]

    await adapter._handle_callback(_press(42, data), context=MagicMock())

    on_message.assert_not_awaited()
    assert audited == ["gateway.adapter.callback_rejected"]


@pytest.mark.asyncio
async def test_a_press_from_an_unadmitted_user_is_dropped() -> None:
    on_message = AsyncMock()
    adapter = _adapter(on_message, allowed=[42])

    await adapter._handle_callback(_press(666, f"/gate {GATE} approve"), context=MagicMock())

    on_message.assert_not_awaited()
