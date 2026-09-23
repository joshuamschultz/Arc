"""Tests for ws_helpers — shared WebSocket patterns."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcui.auth import AuthConfig
from arcui.ws_helpers import (
    AUTH_TIMEOUT_SECONDS,
    CLOSE_AUTH_INVALID,
    CLOSE_AUTH_TIMEOUT,
    CLOSE_CAPACITY_FULL,
    CLOSE_NORMAL,
    MAX_WS_MESSAGE_SIZE,
    authenticate_ws,
    revalidate_ws,
    run_ws_tasks,
)


class TestConstants:
    def test_auth_timeout(self):
        assert AUTH_TIMEOUT_SECONDS == 5.0

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
        ws.receive_text = AsyncMock(return_value=json.dumps({"token": "valid-token"}))
        auth_config = MagicMock()
        auth_config.validate_token.return_value = "viewer"
        auth_config.identify.return_value = None
        ws.app.state.hosted = False

        role, msg = await authenticate_ws(ws, auth_config)
        assert role == "viewer"
        assert msg["token"] == "valid-token"

    @pytest.mark.asyncio
    async def test_invalid_token_returns_none(self):
        ws = AsyncMock()
        ws.receive_text = AsyncMock(return_value=json.dumps({"token": "bad"}))
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


@pytest.mark.asyncio
async def test_session_downgrade_rechecked_before_websocket_action():
    auth = AuthConfig({"viewer_token": "v" * 64, "operator_token": "o" * 64})
    session = auth.sessions.issue(email="boss@example.com", did="did:arc:test:user/abc", role="operator")
    user = SimpleNamespace(did=session.did, disabled=False, is_operator=False)
    ws = AsyncMock()
    ws.app.state = SimpleNamespace(hosted=True, user_store_factory=lambda: SimpleNamespace(get=lambda _: user))
    assert await revalidate_ws(ws, session.token, auth) == "viewer"
    assert auth.identify(session.token).role == "viewer"


@pytest.mark.asyncio
async def test_disabled_session_is_closed_before_websocket_action():
    auth = AuthConfig({"viewer_token": "v" * 64, "operator_token": "o" * 64})
    session = auth.sessions.issue(email="boss@example.com", did="did:arc:test:user/abc", role="operator")
    user = SimpleNamespace(did=session.did, disabled=True, is_operator=True)
    ws = AsyncMock()
    ws.app.state = SimpleNamespace(hosted=True, user_store_factory=lambda: SimpleNamespace(get=lambda _: user))
    assert await revalidate_ws(ws, session.token, auth) is None
    ws.close.assert_awaited_once_with(code=CLOSE_AUTH_INVALID)
    assert auth.identify(session.token) is None


@pytest.mark.asyncio
async def test_account_outage_closes_websocket_without_leaking_error():
    auth = AuthConfig({"viewer_token": "v" * 64, "operator_token": "o" * 64})
    session = auth.sessions.issue(email="boss@example.com", did="did:arc:test:user/abc", role="operator")
    ws = AsyncMock()
    ws.app.state = SimpleNamespace(hosted=True, user_store_factory=lambda: (_ for _ in ()).throw(RuntimeError("sensitive")))
    assert await revalidate_ws(ws, session.token, auth) is None
    assert "sensitive" not in str(ws.send_json.await_args)
