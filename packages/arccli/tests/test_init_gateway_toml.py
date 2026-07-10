"""``arc init``'s generated gateway.toml must use each adapter's real field names.

Regression test for a silent config bug: TelegramPlatformConfig's real field
is ``token_env`` (arcgateway_telegram/config.py), not ``bot_token_env``
(that's Slack's field name — arcgateway_slack/config.py). Both Pydantic
models use ``extra="ignore"``, so a wrong key doesn't raise — it's silently
dropped, and the generated Telegram block ends up authless with no error
until someone notices the bot never receives a token.
"""

from __future__ import annotations

from arccli.commands.init import _generate_gateway_toml


def test_telegram_block_uses_token_env_not_bot_token_env() -> None:
    """The Telegram block must write `token_env`, matching its real config field."""
    toml_text = _generate_gateway_toml("personal")
    assert 'token_env = "TELEGRAM_BOT_TOKEN"' in toml_text
    assert "bot_token_env" not in toml_text.split("[platforms.slack]")[0], (
        "bot_token_env must not appear before [platforms.slack] — "
        "it belongs to Slack's config, not Telegram's"
    )


def test_slack_block_still_uses_bot_token_env() -> None:
    """Slack's real field IS bot_token_env — must not regress the correct one."""
    toml_text = _generate_gateway_toml("personal")
    slack_block = toml_text.split("[platforms.slack]")[1]
    assert 'bot_token_env = "SLACK_BOT_TOKEN"' in slack_block


def test_telegram_token_env_round_trips_through_real_config_model() -> None:
    """The generated key must actually be understood by TelegramPlatformConfig.

    This is the real regression guard: parsing the wrong key doesn't raise
    (extra="ignore"), so only checking the generated TOML text is not
    sufficient — the fix must round-trip through the actual Pydantic model
    the gateway loads at startup.
    """
    import tomllib

    from arcgateway_telegram.config import TelegramPlatformConfig

    toml_text = _generate_gateway_toml("personal")
    parsed = tomllib.loads(toml_text)
    telegram_block = parsed["platforms"]["telegram"]

    cfg = TelegramPlatformConfig.model_validate(telegram_block)
    assert cfg.token_env == "TELEGRAM_BOT_TOKEN"
