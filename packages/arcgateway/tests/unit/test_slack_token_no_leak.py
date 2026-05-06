"""SPEC-025 §L3 — verify SlackAdapter never leaks tokens via repr or audit.

Federal-tier requirement: bot/app tokens must never appear in any string the
audit pipeline, log lines, or operator-visible error message can capture.
This test pins that invariant — if a future change starts including the
token in __repr__, error chains, or debug output, this test fails before
the token reaches any sink.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from arcgateway.adapters.slack import SlackAdapter
from arcgateway.executor import InboundEvent

pytestmark = pytest.mark.asyncio

# Use distinctive tokens so any leak shows up unambiguously in greps.
_BOT_TOKEN = "xoxb-leak-canary-bot-7777777777777777"
_APP_TOKEN = "xapp-leak-canary-app-9999999999999999"


async def _noop_on_message(event: InboundEvent) -> None:
    return None


def _make_adapter(tmp_path: Path) -> SlackAdapter:
    return SlackAdapter(
        bot_token=_BOT_TOKEN,
        app_token=_APP_TOKEN,
        allowed_user_ids=["U_TEST"],
        on_message=_noop_on_message,
        dedup_db_path=tmp_path / "dedup.sqlite",
    )


async def test_slack_adapter_repr_does_not_include_tokens(tmp_path: Path) -> None:
    """``repr(adapter)`` must not contain either the bot or the app token."""
    adapter = _make_adapter(tmp_path)
    rendered = repr(adapter)
    assert _BOT_TOKEN not in rendered, (
        "SlackAdapter repr leaked the bot token — secrets must never appear in repr"
    )
    assert _APP_TOKEN not in rendered, (
        "SlackAdapter repr leaked the app token"
    )


async def test_slack_adapter_str_does_not_include_tokens(tmp_path: Path) -> None:
    """str(adapter) must also be safe — covers the f-string in log lines path."""
    adapter = _make_adapter(tmp_path)
    rendered = str(adapter)
    assert _BOT_TOKEN not in rendered
    assert _APP_TOKEN not in rendered


async def test_slack_adapter_dir_does_not_expose_token_attr_with_obvious_name(
    tmp_path: Path,
) -> None:
    """Public attrs that scream 'token' would invite logging/repr leaks.

    The canonical Python pattern is to use a leading underscore on the
    attribute name (``self._bot_token``, ``self._app_token``). This test
    pins that convention — any future PR that adds a public ``bot_token``
    attribute will fail here before it can leak through dataclass repr.
    """
    adapter = _make_adapter(tmp_path)
    public = {a for a in dir(adapter) if not a.startswith("_")}
    assert "bot_token" not in public, (
        "Public 'bot_token' attribute would leak via dataclass repr / serializers"
    )
    assert "app_token" not in public, (
        "Public 'app_token' attribute would leak via dataclass repr / serializers"
    )


async def test_slack_adapter_constructor_validation_error_does_not_echo_token(
    tmp_path: Path,
) -> None:
    """The xoxb-/xapp- prefix validation message must not include the token.

    SlackAdapter raises on bad prefix; the error message must reveal the
    prefix shape but never the actual token bytes — otherwise a misconfigured
    deploy logs the secret to stderr.
    """
    bad_token = "xoxp-not-a-bot-token-secret-leak-canary"
    with pytest.raises(ValueError) as exc_info:
        SlackAdapter(
            bot_token=bad_token,
            app_token=_APP_TOKEN,
            allowed_user_ids=["U_TEST"],
            on_message=_noop_on_message,
            dedup_db_path=tmp_path / "dedup.sqlite",
        )
    msg = str(exc_info.value)
    assert bad_token not in msg, (
        f"Validation error echoed the token: {msg!r}"
    )


async def test_slack_adapter_logging_handlers_do_not_emit_tokens(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Any log records emitted at construction must not contain tokens."""
    caplog.set_level(logging.DEBUG, logger="arcgateway.adapters.slack")
    _ = _make_adapter(tmp_path)
    for record in caplog.records:
        assert _BOT_TOKEN not in record.getMessage(), (
            f"Bot token leaked in log record: {record.getMessage()!r}"
        )
        assert _APP_TOKEN not in record.getMessage(), (
            f"App token leaked in log record: {record.getMessage()!r}"
        )
