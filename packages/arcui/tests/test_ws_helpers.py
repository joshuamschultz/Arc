"""Tests for ws_helpers — shared WebSocket patterns."""

from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcui.ws_helpers import (
    AUTH_TIMEOUT_SECONDS,
    CLOSE_AUTH_INVALID,
    CLOSE_AUTH_TIMEOUT,
    CLOSE_CAPACITY_FULL,
    CLOSE_NORMAL,
    HEARTBEAT_INTERVAL_SECONDS,
    MAX_WS_MESSAGE_SIZE,
    authenticate_ws,
    heartbeat_loop,
    run_ws_tasks,
    safe_enqueue,
)


class TestConstants:
    def test_auth_timeout(self):
        assert AUTH_TIMEOUT_SECONDS == 5.0

    def test_heartbeat_interval(self):
        assert HEARTBEAT_INTERVAL_SECONDS == 30.0

    def test_max_message_size(self):
        assert MAX_WS_MESSAGE_SIZE == 1_048_576

    def test_close_codes(self):
        assert CLOSE_NORMAL == 1000
        assert CLOSE_AUTH_TIMEOUT == 4001
        assert CLOSE_AUTH_INVALID == 4003
        assert CLOSE_CAPACITY_FULL == 4029


class TestAuthenticateWS:
    @pytest.mark.asyncio
    async def test_valid_token_returns_role(self):
        ws = AsyncMock()
        ws.receive_text = AsyncMock(
            return_value=json.dumps({"token": "valid-token"})
        )
        auth_config = MagicMock()
        auth_config.validate_token.return_value = "viewer"

        role, msg = await authenticate_ws(ws, auth_config)
        assert role == "viewer"
        assert msg["token"] == "valid-token"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_none(self):
        ws = AsyncMock()
        ws.receive_text = AsyncMock(
            return_value=json.dumps({"token": "bad"})
        )
        auth_config = MagicMock()
        auth_config.validate_token.return_value = None

        role, msg = await authenticate_ws(ws, auth_config)
        assert role is None
        assert msg == {}
        ws.send_json.assert_called_once()
        ws.close.assert_called_once_with(code=CLOSE_AUTH_INVALID)

    @pytest.mark.asyncio
    async def test_timeout_returns_none(self):
        ws = AsyncMock()
        ws.receive_text = AsyncMock(side_effect=TimeoutError)
        auth_config = MagicMock()

        role, msg = await authenticate_ws(ws, auth_config)
        assert role is None
        assert msg == {}
        ws.close.assert_called_once_with(code=CLOSE_AUTH_TIMEOUT)

    @pytest.mark.asyncio
    async def test_invalid_json_returns_none(self):
        ws = AsyncMock()
        ws.receive_text = AsyncMock(return_value="not-json")
        auth_config = MagicMock()

        role, msg = await authenticate_ws(ws, auth_config)
        assert role is None
        assert msg == {}

    @pytest.mark.asyncio
    async def test_require_role_matches(self):
        ws = AsyncMock()
        ws.receive_text = AsyncMock(
            return_value=json.dumps({"token": "agent-tok"})
        )
        auth_config = MagicMock()
        auth_config.validate_token.return_value = "agent"

        role, _msg = await authenticate_ws(ws, auth_config, require_role="agent")
        assert role == "agent"

    @pytest.mark.asyncio
    async def test_require_role_mismatch(self):
        ws = AsyncMock()
        ws.receive_text = AsyncMock(
            return_value=json.dumps({"token": "viewer-tok"})
        )
        auth_config = MagicMock()
        auth_config.validate_token.return_value = "viewer"

        role, _msg = await authenticate_ws(ws, auth_config, require_role="agent")
        assert role is None
        ws.close.assert_called_once_with(code=CLOSE_AUTH_INVALID)


class TestSafeEnqueue:
    def test_normal_enqueue(self):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=5)
        safe_enqueue(queue, "msg1")
        assert queue.qsize() == 1

    def test_drops_oldest_when_full(self):
        queue: asyncio.Queue[str] = asyncio.Queue(maxsize=2)
        safe_enqueue(queue, "msg1")
        safe_enqueue(queue, "msg2")
        safe_enqueue(queue, "msg3")
        assert queue.qsize() == 2
        # Oldest (msg1) should have been dropped
        assert queue.get_nowait() == "msg2"


class TestHeartbeatLoop:
    @pytest.mark.asyncio
    async def test_heartbeat_sends_ping(self):
        ws = AsyncMock()
        call_count = 0

        async def fake_sleep(duration):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                raise RuntimeError("stop")

        original_sleep = asyncio.sleep
        asyncio.sleep = fake_sleep
        try:
            await heartbeat_loop(ws)
        finally:
            asyncio.sleep = original_sleep

        assert ws.send_json.call_count >= 1
        ws.send_json.assert_called_with({"type": "ping"})


class TestRunWSTasks:
    @pytest.mark.asyncio
    async def test_cancels_remaining_on_first_complete(self):
        completed = asyncio.Event()

        async def fast():
            completed.set()

        async def slow():
            await asyncio.sleep(100)

        done, pending = await run_ws_tasks(fast(), slow())
        assert len(done) == 1
        assert len(pending) == 1
