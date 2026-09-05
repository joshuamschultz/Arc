"""T-017 — the v1 push-to-talk WebSocket transport (SPEC-077 COMP-007, REQ-009).

Real localhost loopback: an authenticated client sends one utterance and gets one
reply; a bad token is refused at the handshake. (WebRTC + mTLS is the follow-up;
here we prove the pairing-token gate and the one-utterance/one-reply contract.)
"""

from __future__ import annotations

import pytest

from arcgateway.adapters.voice.transport import VoiceClient, VoiceLink, VoiceServer


def _auth_good(token: str) -> tuple[str, str] | None:
    return ("did:user:josh", "mic:desk") if token == "good-token" else None


async def test_authenticated_utterance_gets_one_reply() -> None:
    seen: dict[str, object] = {}

    async def on_utterance(link: VoiceLink, pcm: bytes) -> None:
        seen["pcm"] = pcm
        seen["chat_id"] = link.chat_id
        seen["user_did"] = link.user_did
        await link.send_audio(b"RIFF-reply-wav")

    server = VoiceServer(
        host="127.0.0.1", port=0, authenticate=_auth_good, on_utterance=on_utterance
    )
    await server.start()
    try:
        client = VoiceClient(uri=f"ws://127.0.0.1:{server.bound_port()}", token="good-token")
        await client.connect()
        assert client.chat_id == "mic:desk"
        reply = await client.send_utterance(b"\x01\x02utterance-pcm")
        assert reply == b"RIFF-reply-wav"
        assert seen["pcm"] == b"\x01\x02utterance-pcm"
        assert seen["chat_id"] == "mic:desk"
        assert seen["user_did"] == "did:user:josh"
        await client.close()
    finally:
        await server.stop()


async def test_bad_token_is_refused() -> None:
    async def on_utterance(link: VoiceLink, pcm: bytes) -> None:  # pragma: no cover
        raise AssertionError("must not be reached on a refused token")

    server = VoiceServer(
        host="127.0.0.1", port=0, authenticate=_auth_good, on_utterance=on_utterance
    )
    await server.start()
    try:
        client = VoiceClient(uri=f"ws://127.0.0.1:{server.bound_port()}", token="wrong")
        with pytest.raises(PermissionError):
            await client.connect()
    finally:
        await server.stop()
