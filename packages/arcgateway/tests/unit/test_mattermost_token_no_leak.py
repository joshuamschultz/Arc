"""SPEC-025 §L3 — verify MattermostAdapter never leaks the PAT via repr or audit.

Federal-tier requirement: Personal Access Tokens must never appear in any
string the audit pipeline, log lines, or operator-visible error messages can
capture.  This file mirrors the structure of test_slack_token_no_leak.py.
"""

from __future__ import annotations

import logging

import pytest

from arcgateway.adapters.mattermost import MattermostAdapter
from arcgateway.executor import InboundEvent

pytestmark = pytest.mark.asyncio

# Use distinctive token so any leak shows up unambiguously in greps.
_PAT = "mm-pat-leak-canary-bot-777777777777777"


async def _noop_on_message(event: InboundEvent) -> None:
    return None


def _make_adapter() -> MattermostAdapter:
    return MattermostAdapter(
        server_url="http://localhost:8065",
        bot_token=_PAT,
        on_message=_noop_on_message,
    )


async def test_mattermost_adapter_repr_does_not_include_token() -> None:
    """``repr(adapter)`` must not contain the PAT."""
    adapter = _make_adapter()
    rendered = repr(adapter)
    assert _PAT not in rendered, (
        "MattermostAdapter repr leaked the PAT — secrets must never appear in repr"
    )


async def test_mattermost_adapter_str_does_not_include_token() -> None:
    """str(adapter) must also be safe — covers f-string in log lines path."""
    adapter = _make_adapter()
    rendered = str(adapter)
    assert _PAT not in rendered


async def test_mattermost_adapter_dir_does_not_expose_token_attr() -> None:
    """Public attrs must not include 'bot_token' to prevent accidental leakage.

    The canonical Python convention is to use a leading underscore on the
    private attribute (``self._bot_token``). This test pins that convention.
    Any future PR that promotes the attribute to public will fail here before
    the token can leak through dataclass repr or serializers.
    """
    adapter = _make_adapter()
    public = {a for a in dir(adapter) if not a.startswith("_")}
    assert "bot_token" not in public, (
        "Public 'bot_token' attribute would leak via dataclass repr / serializers"
    )


async def test_mattermost_adapter_federal_error_does_not_echo_token() -> None:
    """The federal-tier ValueError message must not include the PAT."""
    from unittest.mock import patch

    with patch(
        "arcgateway.adapters.mattermost.socket.getaddrinfo",
        return_value=[(None, None, None, None, ("1.2.3.4", 0))],
    ):
        with pytest.raises(ValueError) as exc_info:
            MattermostAdapter(
                server_url="https://public.example.com",
                bot_token=_PAT,
                on_message=_noop_on_message,
                tier="federal",
            )
        msg = str(exc_info.value)
        assert _PAT not in msg, (
            f"Federal validation error echoed the PAT: {msg!r}"
        )


async def test_mattermost_adapter_logging_does_not_emit_token(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log records emitted at construction must not contain the PAT."""
    caplog.set_level(logging.DEBUG, logger="arcgateway.adapters.mattermost")
    _ = _make_adapter()
    for record in caplog.records:
        assert _PAT not in record.getMessage(), (
            f"PAT leaked in log record: {record.getMessage()!r}"
        )


# SPEC-025 §L-3 — HTTP failure body must not echo the bearer token


async def test_post_failure_does_not_echo_response_body(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A 401/403 response body could contain the bearer header on
    misconfigured servers; ensure we never log it.

    Previously the failure log included ``body=text[:200]`` — even with
    truncation, an echoing server returns the bearer token early in the
    response. The L-3 fix removes the body from the failure log entirely.
    """
    import logging
    from unittest.mock import AsyncMock, MagicMock, patch

    from arcgateway.adapters.mattermost import MattermostAdapter

    bot_token = "mm-leak-canary-XXXXXXXXXXXXXX"

    async def _on_msg(_e: Any) -> None:
        pass

    adapter = MattermostAdapter(
        server_url="http://localhost:8065",
        bot_token=bot_token,
        on_message=_on_msg,
    )

    # Construct a fake 401 response whose body echoes the bearer header.
    fake_resp = MagicMock()
    fake_resp.status = 401
    fake_resp.text = AsyncMock(return_value=f"Invalid token Bearer {bot_token}")

    fake_session = MagicMock()
    # session.post(...) is a context manager; .__aenter__ → fake_resp
    fake_post_cm = MagicMock()
    fake_post_cm.__aenter__ = AsyncMock(return_value=fake_resp)
    fake_post_cm.__aexit__ = AsyncMock(return_value=None)
    fake_session.post = MagicMock(return_value=fake_post_cm)
    fake_session.closed = False
    fake_session.close = AsyncMock()

    with patch.object(adapter, "_ensure_http_session", AsyncMock(return_value=fake_session)):
        with caplog.at_level(logging.WARNING, logger="arcgateway.adapters.mattermost"):
            await adapter._post_message("channel-1", "hello")

    # Confirm at least one warning was emitted for the failure, and that
    # NONE of the records contain the bearer token bytes.
    failure_records = [
        r for r in caplog.records
        if "POST /api/v4/posts failed" in r.getMessage()
    ]
    assert len(failure_records) >= 1, "warning log should fire on POST failure"
    for record in caplog.records:
        assert bot_token not in record.getMessage(), (
            f"Bot token leaked in log record: {record.getMessage()!r}"
        )
