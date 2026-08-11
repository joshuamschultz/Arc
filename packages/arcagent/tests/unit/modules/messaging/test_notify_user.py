"""notify_user — the channel-agnostic agent→human tool (re-homed from the
removed in-agent telegram module).

Routes through the gateway's channel delivery (``channel_deliver_fn``) to the
current turn's channel, falling back to the agent's most-recently-seen channel.
No per-platform bot lives in the agent anymore.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from arctrust import AgentIdentity
from packages.arcagent.tests.unit.modules.messaging.conftest import (
    make_config_dict,
    make_operator_signer,
)

from arcagent.core import known_channels, turn_context
from arcagent.modules.messaging import _runtime
from arcagent.modules.messaging.capabilities import notify_user


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    turn_context.set_inbound_channel(None)
    yield
    _runtime.reset()
    turn_context.set_inbound_channel(None)


def _configure(tmp_path: Path) -> Any:
    _runtime.configure(
        config=make_config_dict(entity_id="agent://me"),
        workspace=tmp_path,
        identity=AgentIdentity.generate(org="local", agent_type="agent"),
        operator_signer=make_operator_signer(),
    )
    return _runtime.state()


class TestNotifyUser:
    @pytest.mark.asyncio
    async def test_delivers_to_current_turn_channel(self, tmp_path: Path) -> None:
        st = _configure(tmp_path)
        st.channel_deliver_fn = AsyncMock()
        turn_context.set_inbound_channel("telegram:5")

        out = json.loads(await notify_user(message="done!"))

        assert out["status"] == "sent"
        assert out["target"] == "telegram:5"
        st.channel_deliver_fn.assert_awaited_once_with("telegram:5", "done!")

    @pytest.mark.asyncio
    async def test_falls_back_to_most_recent_known_channel(self, tmp_path: Path) -> None:
        st = _configure(tmp_path)
        st.channel_deliver_fn = AsyncMock()
        known_channels.record(tmp_path, target="telegram:9", label="Telegram — Josh")
        # No current turn channel set -> use the known one.

        out = json.loads(await notify_user(message="hi"))

        assert out["target"] == "telegram:9"
        st.channel_deliver_fn.assert_awaited_once_with("telegram:9", "hi")

    @pytest.mark.asyncio
    async def test_no_known_channel_returns_error(self, tmp_path: Path) -> None:
        st = _configure(tmp_path)
        st.channel_deliver_fn = AsyncMock()

        out = json.loads(await notify_user(message="hi"))

        assert "error" in out
        st.channel_deliver_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_no_delivery_fn_returns_error(self, tmp_path: Path) -> None:
        st = _configure(tmp_path)
        st.channel_deliver_fn = None
        turn_context.set_inbound_channel("telegram:5")

        out = json.loads(await notify_user(message="hi"))

        assert "error" in out

    @pytest.mark.asyncio
    async def test_empty_message_rejected(self, tmp_path: Path) -> None:
        st = _configure(tmp_path)
        st.channel_deliver_fn = AsyncMock()
        turn_context.set_inbound_channel("telegram:5")

        out = json.loads(await notify_user(message="   "))

        assert "error" in out
        st.channel_deliver_fn.assert_not_called()
