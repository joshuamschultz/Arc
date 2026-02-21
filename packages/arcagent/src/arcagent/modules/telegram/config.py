"""Configuration for the Telegram messaging module.

Owned by the telegram module — not part of core config.
Loaded from ``[modules.telegram.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from arcagent.modules.base_config import ModuleConfig


class TelegramConfig(ModuleConfig):
    """Telegram module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    enabled: bool = False
    allowed_chat_ids: list[int] = []  # noqa: RUF012 — Pydantic handles mutable defaults
    poll_interval: float = 1.0
    max_message_length: int = 4096
    bot_token_env_var: str = "ARCAGENT_TELEGRAM_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
