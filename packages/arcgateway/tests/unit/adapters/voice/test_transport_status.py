"""Item 13 — live status over the voice link: hello/state, stale, cmd, trust.

Real localhost loopback with a controllable clock, so staleness is exact.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from websockets.asyncio.client import connect

from arcgateway.adapters.voice.adapter import VoiceAdapter
from arcgateway.adapters.voice.config import WakeConfig
from arcgateway.adapters.voice.engine.base import FakeVoiceEngine
from arcgateway.adapters.voice.status import STALE_AFTER_SECONDS


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


async def _noop(_draft: object) -> None:
    return None


def _auth(token: str) -> tuple[str, str] | None:
    return ("did:user:josh", "olivia") if token == "good" else None


@pytest.fixture
async def adapter() -> AsyncIterator[VoiceAdapter]:
    voice = VoiceAdapter(
        on_message=_noop,
        agent_did="did:arc:olivia",
        engine=FakeVoiceEngine(),
        port=0,
        authenticate=_auth,
        chat_id="olivia",
        engine_names=("whisper", "kokoro"),
    )
    await voice.connect()
    yield voice
    await voice.disconnect()


async def _login(voice: VoiceAdapter, token: str = "good") -> Any:  # noqa: S107 - test token
    ws = await connect(f"ws://127.0.0.1:{voice.bound_port()}")
    await ws.send(json.dumps({"t": "auth", "token": token}))
    return ws


async def _settle() -> None:
    await asyncio.sleep(0.05)


async def test_hello_and_state_update_link_record(adapter: VoiceAdapter) -> None:
    ws = await _login(adapter)
    ready = json.loads(await ws.recv())
    assert ready["t"] == "ready"
    assert ready["listening"] is True
    assert ready["wake"]["words"] == ["olivia"]  # defaults to the channel name

    await ws.send(
        json.dumps(
            {
                "t": "hello",
                "mode": "stt-wake",
                "mic": "plughw:MV7i",
                "wake": "olivia",
                "wake_loaded": True,
            }
        )
    )
    await ws.send(json.dumps({"t": "state", "state": "speaking", "last_wake_at": 1234.5}))
    await _settle()

    live = adapter.status()
    assert live.client_connected is True
    assert live.state == "speaking"
    assert live.mode == "stt-wake"
    assert live.mic == "plughw:MV7i"
    assert live.wake_loaded is True
    assert live.last_wake_at == 1234.5
    assert live.client.up is True
    assert live.adapter.up is True
    assert live.engine.up is True
    assert live.reason == ""
    await ws.close()


async def test_stale_link_reports_offline(adapter: VoiceAdapter) -> None:
    clock = _Clock()
    adapter._server._clock = clock  # type: ignore[attr-defined]  # reason: deterministic time
    ws = await _login(adapter)
    await ws.recv()
    await ws.send(json.dumps({"t": "state", "state": "idle"}))
    await _settle()
    assert adapter.status(now=clock.now).client_connected is True

    clock.now += STALE_AFTER_SECONDS + 1
    stale = adapter.status(now=clock.now)
    assert stale.client_connected is False
    assert stale.state == "offline"
    assert "stopped reporting" in stale.reason
    await ws.close()


async def test_no_client_says_how_to_start_one(adapter: VoiceAdapter) -> None:
    live = adapter.status()
    assert live.client_connected is False
    assert live.state == "offline"
    assert "mic app" in live.reason
    assert live.adapter.up is True


async def test_cmd_pause_delivered_and_unauth_link_cannot_send_state(
    adapter: VoiceAdapter,
) -> None:
    good = await _login(adapter)
    await good.recv()
    bad = await _login(adapter, token="wrong")
    assert json.loads(await bad.recv())["t"] == "denied"
    # an unauthenticated socket tries to report and to receive control
    with pytest.raises(Exception):  # server closed the refused link
        await bad.send(json.dumps({"t": "state", "state": "speaking"}))
        await bad.recv()

    told = await adapter.set_listening(False, actor_did="did:user:op")
    assert told == 1  # only the authenticated link
    cmd = json.loads(await asyncio.wait_for(good.recv(), 1))
    assert cmd == {"t": "cmd", "listening": False}
    assert adapter.listening is False
    await _settle()
    assert adapter.status().state == "paused"
    assert len(adapter._server.links()) == 1  # the refused link never became a client
    await good.close()


async def test_client_cannot_change_wake_words(adapter: VoiceAdapter) -> None:
    ws = await _login(adapter)
    await ws.recv()
    await ws.send(json.dumps({"t": "wake", "words": ["pwned"]}))
    await ws.send(json.dumps({"t": "hello", "mode": "stt-wake", "wake": ["pwned"]}))
    await ws.send(json.dumps({"t": "cmd", "wake": {"words": ["pwned"]}}))
    await _settle()
    assert adapter.status().wake_words == ["olivia"]
    await ws.close()


async def test_set_wake_is_pushed_live_and_reported(adapter: VoiceAdapter) -> None:
    ws = await _login(adapter)
    await ws.recv()
    await adapter.set_wake(WakeConfig(words=["computer"], match="exact"), actor_did="did:user:op")
    cmd = json.loads(await asyncio.wait_for(ws.recv(), 1))
    assert cmd["wake"]["words"] == ["computer"]
    assert cmd["wake"]["match"] == "exact"
    assert adapter.status().wake_words == ["computer"]
    await ws.close()


async def test_oversized_and_malformed_control_frames_are_ignored(
    adapter: VoiceAdapter,
) -> None:
    ws = await _login(adapter)
    await ws.recv()
    await ws.send(json.dumps({"t": "state", "state": "speaking", "pad": "x" * 5000}))
    await ws.send("not json")
    await ws.send(json.dumps({"t": "state", "state": "<script>"}))
    await _settle()
    assert adapter.status().state == "listening"  # untouched default, junk never applied
    await ws.close()


async def test_status_has_no_transcript_outside_a_test(adapter: VoiceAdapter) -> None:
    ws = await _login(adapter)
    await ws.recv()
    await ws.send(json.dumps({"t": "state", "state": "heard", "heard": "secret words"}))
    await _settle()
    dumped = adapter.status().model_dump_json()
    assert "secret words" not in dumped
    assert adapter.status().test_heard is None

    await adapter.start_test(actor_did="did:user:op")
    await ws.send(json.dumps({"t": "state", "state": "heard", "heard": "hello there"}))
    await _settle()
    assert adapter.status().test_heard == "hello there"
    await ws.close()
