"""Configuration for the Slack messaging module.

Owned by the slack module — not part of core config.
Loaded from ``[modules.slack.config]`` in arcagent.toml.
Validated internally by the module on construction.
"""

from __future__ import annotations

from arcagent.modules.base_config import ModuleConfig


class SlackConfig(ModuleConfig):
    """Slack module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    enabled: bool = False
    allowed_user_ids: list[str] = []  # noqa: RUF012 — Pydantic handles mutable defaults
    max_message_length: int = 4000
    bot_token_env_var: str = "ARCAGENT_SLACK_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
    app_token_env_var: str = "ARCAGENT_SLACK_APP_TOKEN"  # noqa: S105 — env var name, not a secret
    downloads_dir: str = "files"
    max_file_size_mb: int = 20
    allowed_extensions: list[str] = []  # noqa: RUF012 — empty = allow all
