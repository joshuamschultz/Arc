"""The two things an inbound artefact must never be allowed to do.

SPEC-065 review findings. Both live at the same seam — the moment an adapter
turns a remote payload field into an outbound HTTP request — and both are
invisible to every per-layer unit test that mocks the transport.

1. **Bleed the bot token.** A Slack file object names its own download URL.
   That URL is remote input, and the adapter attaches the bot's bearer token to
   it. Without a host check, one crafted payload field exfiltrates the
   workspace token to an attacker's server (SEC-26 / LLM02).

2. **Exhaust memory.** The size ceiling is the gateway's (REQ-310), but it can
   only refuse bytes it has already paid for unless the adapter is told the
   bound *before* it reads. A 1GB attachment must be refused, not resident
   (SEC-27 / LLM10).
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from arcgateway.adapters.base import MediaTooLargeOnWireError
from arcgateway.adapters.mattermost.adapter import MattermostAdapter
from arcgateway.adapters.slack.adapter import SlackAdapter

_BOT_TOKEN = "xoxb-sentinel-token-000"
_APP_TOKEN = "xapp-sentinel-token-000"


async def _noop(_draft: Any) -> None:
    return None


def _slack() -> SlackAdapter:
    return SlackAdapter(
        bot_token=_BOT_TOKEN,
        app_token=_APP_TOKEN,
        allowed_user_ids=["U1"],
        on_message=_noop,
        agent_did="did:arc:alpha",
    )


class _Response:
    """An aiohttp-ish response whose body arrives in chunks."""

    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks
        self.content = self
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        return None

    async def iter_chunked(self, _size: int) -> Any:
        for chunk in self._chunks:
            yield chunk

    async def __aenter__(self) -> _Response:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


class _Session:
    """Records every URL and header the adapter sends."""

    closed = False

    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, dict[str, str]]] = []

    def get(self, url: str, headers: dict[str, str] | None = None, **_: object) -> _Response:
        self.calls.append((url, dict(headers or {})))
        return _Response(self.chunks)


# --- 1. the token never leaves for a host Slack does not own ----------------


@pytest.mark.parametrize(
    "hostile_url",
    [
        "https://attacker.example/files/steal",
        "http://slack.com.attacker.example/x",
        "https://files.slack.com.evil.test/x",
        "https://notslack.com/files/x",
        "http://127.0.0.1:9/x",
    ],
)
async def test_a_download_url_off_slack_never_receives_the_bot_token(
    hostile_url: str,
) -> None:
    """The URL is remote input; the credential is not."""
    adapter = _slack()
    session = _Session([b"x"])
    adapter._http_session = session  # type: ignore[assignment]

    with pytest.raises(ValueError, match="not a Slack file host"):
        await adapter._download(hostile_url, "F1", limit_bytes=1024)

    assert session.calls == [], (
        f"the adapter contacted {hostile_url} — and every call carries the bot "
        "token, so reaching the host at all is the leak"
    )


@pytest.mark.parametrize(
    "good_url",
    [
        "https://files.slack.com/files-pri/T1-F1/photo.png",
        "https://myteam.slack.com/files/U1/F1/photo.png",
    ],
)
async def test_a_genuine_slack_url_is_still_fetched(good_url: str) -> None:
    adapter = _slack()
    session = _Session([b"bytes"])
    adapter._http_session = session  # type: ignore[assignment]

    assert await adapter._download(good_url, "F1", limit_bytes=1024) == b"bytes"
    assert session.calls[0][1]["Authorization"] == f"Bearer {_BOT_TOKEN}"


# --- 2. the bytes stop arriving at the ceiling ------------------------------


async def test_slack_stops_reading_once_the_ceiling_is_passed() -> None:
    """A 1GB file must not become 1GB of resident memory before refusal."""
    adapter = _slack()
    # Ten 1MB chunks behind a 2MB ceiling: a bounded read touches three.
    session = _Session([b"\x00" * 1_000_000 for _ in range(10)])
    adapter._http_session = session  # type: ignore[assignment]

    with pytest.raises(MediaTooLargeOnWireError):
        await adapter._download(
            "https://files.slack.com/files-pri/T1-F1/big.bin", "F1", limit_bytes=2_000_000
        )


async def test_mattermost_stops_reading_once_the_ceiling_is_passed() -> None:
    adapter = MattermostAdapter(
        server_url="https://chat.example",
        bot_token="mm-token",
        on_message=_noop,
        agent_did="did:arc:alpha",
    )
    session = _Session([b"\x00" * 1_000_000 for _ in range(10)])
    adapter._http_session = session  # type: ignore[assignment]

    with pytest.raises(MediaTooLargeOnWireError):
        await adapter._download("F1", limit_bytes=2_000_000)


async def test_a_file_under_the_ceiling_still_arrives_whole() -> None:
    adapter = _slack()
    session = _Session([b"a" * 100, b"b" * 100])
    adapter._http_session = session  # type: ignore[assignment]

    body = await adapter._download(
        "https://files.slack.com/files-pri/T1-F1/small.bin", "F1", limit_bytes=1024
    )
    assert body == b"a" * 100 + b"b" * 100


async def test_telegram_refuses_an_oversize_file_before_downloading_it() -> None:
    """Telegram states the size on the handle; reading it is free."""
    from arcgateway.adapters.telegram.adapter import TelegramAdapter

    adapter = TelegramAdapter(
        bot_token="1:AAA",
        allowed_user_ids=[1],
        on_message=_noop,
        agent_did="did:arc:alpha",
    )
    handle = MagicMock()
    handle.file_size = 500_000_000
    handle.download_as_bytearray = AsyncMock(return_value=bytearray(b"never"))
    bot = MagicMock()
    bot.get_file = AsyncMock(return_value=handle)
    application = MagicMock()
    application.bot = bot
    adapter._application = application  # type: ignore[assignment]

    attachment = MagicMock()
    attachment.file_id = "F1"
    pending = adapter._pending(attachment, "file", "application/pdf", "big.pdf")

    with pytest.raises(MediaTooLargeOnWireError):
        await pending.fetch(20 * 1024 * 1024)

    handle.download_as_bytearray.assert_not_awaited()
