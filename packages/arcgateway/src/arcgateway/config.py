"""GatewayConfig — Pydantic model for gateway.toml configuration.

Sections mirror the TOML structure used by ``arc gateway start --config``:

    [gateway]
    tier = "personal"        # "personal" | "enterprise" | "federal"
    agent_did = "did:arc:agent:default"
    runtime_dir = "~/.arc/gateway/run"

    [security]
    require_pairing = false  # Require DM pairing before routing to agent

    [platforms.telegram]
    enabled = true
    token_env = "TELEGRAM_BOT_TOKEN"   # Env var name (never inline the token)
    allowed_user_ids = [123456789]

    [platforms.slack]
    enabled = true
    bot_token_env = "SLACK_BOT_TOKEN"
    app_token_env = "SLACK_APP_TOKEN"
    allowed_user_ids = ["UABC123"]

    [platforms.mattermost]
    enabled = true
    server_url = "https://mattermost.internal.example.gov"
    bot_token_env = "MM_BOT_TOKEN"
    allowed_channel_ids = ["channelid1", "channelid2"]
    intranet_domains = ["mattermost.internal.example.gov"]

    [pairing]
    db_path = "~/.arc/gateway/pairing.db"

Tier policy (SDD §3.1 Platform Credentials):
    personal   — tokens from env or file; no vault required.
    enterprise — tokens from vault preferred; env fallback with warn.
    federal    — tokens MUST come from vault; hard error if vault unreachable.
                 (VaultUnreachable check is in cmd_start, not here)

Design:
    All token values are read from environment variables at runtime — the
    config file stores only the *name* of the env var, never the value.
    This keeps secrets off disk (NIST 800-53 SC-28 / CMMC MP.3).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field, model_validator

_logger = logging.getLogger("arcgateway.config")


# ---------------------------------------------------------------------------
# Section models
# ---------------------------------------------------------------------------


class GatewaySection(BaseModel):
    """[gateway] section."""

    tier: Literal["personal", "enterprise", "federal"] = "personal"
    agent_did: str = "did:arc:agent:default"
    runtime_dir: Path = Path("~/.arc/gateway/run")

    @model_validator(mode="after")
    def _expand_paths(self) -> GatewaySection:
        self.runtime_dir = Path(str(self.runtime_dir)).expanduser().resolve()
        return self


class SecuritySection(BaseModel):
    """[security] section."""

    require_pairing: bool = False


class TelegramPlatformConfig(BaseModel):
    """[platforms.telegram] section."""

    enabled: bool = False
    token_env: str = "TELEGRAM_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
    allowed_user_ids: list[int] = Field(default_factory=list)
    agent_did: str = ""  # Overrides [gateway].agent_did for this platform

    def resolve_token(self) -> str | None:
        """Read bot token from the configured env var.

        Returns None if the env var is not set (so caller can handle
        the tier-appropriate error: hard fail at federal, warn at enterprise,
        skip at personal).
        """
        return os.environ.get(self.token_env)


class SlackPlatformConfig(BaseModel):
    """[platforms.slack] section."""

    enabled: bool = False
    bot_token_env: str = "SLACK_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
    app_token_env: str = "SLACK_APP_TOKEN"  # noqa: S105 — env var name, not a secret
    allowed_user_ids: list[str] = Field(default_factory=list)
    agent_did: str = ""  # Overrides [gateway].agent_did for this platform

    def resolve_bot_token(self) -> str | None:
        """Read bot token from env var."""
        return os.environ.get(self.bot_token_env)

    def resolve_app_token(self) -> str | None:
        """Read app token from env var."""
        return os.environ.get(self.app_token_env)


class MattermostPlatformConfig(BaseModel):
    """[platforms.mattermost] section.

    Mattermost adapter for air-gapped DOE/National Lab deployments
    (FedRAMP High / IL5 / JWICS). Authenticates via a Personal Access
    Token (PAT); the token value is read from an environment variable at
    runtime — never stored inline.

    Fields:
        enabled: Whether the adapter is active.
        server_url: Base HTTPS URL of the Mattermost server, e.g.
            ``https://mattermost.internal.doe.gov``.  No trailing slash.
        bot_token_env: Name of the env var holding the PAT.
        allowed_channel_ids: Channel IDs the bot accepts messages from.
            Empty list = DMs only (conservative default).
        bot_user_id: Mattermost user ID of the bot; used to skip own posts.
            Leave empty to disable self-filtering.
        intranet_domains: Additional hostnames treated as private for the
            federal-tier air-gap guard even if they don't resolve to RFC 1918
            addresses (e.g. ``mattermost.internal.doe.gov``).
        agent_did: Overrides [gateway].agent_did for this platform.
    """

    enabled: bool = False
    server_url: str = ""
    bot_token_env: str = "MM_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
    allowed_channel_ids: list[str] = Field(default_factory=list)
    bot_user_id: str = ""
    intranet_domains: list[str] = Field(default_factory=list)
    agent_did: str = ""  # Overrides [gateway].agent_did for this platform

    def resolve_bot_token(self) -> str | None:
        """Read the PAT from the configured env var.

        Returns None if unset so the caller can apply tier-appropriate
        error handling (hard fail at federal, warn at enterprise, skip at
        personal).
        """
        return os.environ.get(self.bot_token_env)


class WebPlatformConfig(BaseModel):
    """[platforms.web] section.

    The web platform is the in-process browser chat adapter (SPEC-023).
    Unlike Telegram/Slack, it has no remote token: arcui hosts the gateway
    runtime and routes browser WebSocket connections directly into the
    adapter.

    Bounds (validated by Pydantic Field constraints):
        - max_connections: 1..10000
        - idle_timeout_seconds: 60..86400
        - max_frame_bytes: 1024..1048576
    """

    enabled: bool = False
    agent_did: str = ""  # Overrides [gateway].agent_did for this platform.
    max_connections: int = Field(default=50, ge=1, le=10_000)
    idle_timeout_seconds: int = Field(default=3600, ge=60, le=86_400)
    max_frame_bytes: int = Field(default=65_536, ge=1024, le=1_048_576)


class PlatformsSection(BaseModel):
    """[platforms] section containing per-platform configs."""

    telegram: TelegramPlatformConfig = Field(default_factory=TelegramPlatformConfig)
    slack: SlackPlatformConfig = Field(default_factory=SlackPlatformConfig)
    mattermost: MattermostPlatformConfig = Field(
        default_factory=MattermostPlatformConfig
    )
    web: WebPlatformConfig = Field(default_factory=WebPlatformConfig)


class PairingSection(BaseModel):
    """[pairing] section."""

    db_path: Path = Path("~/.arc/gateway/pairing.db")

    @model_validator(mode="after")
    def _expand_paths(self) -> PairingSection:
        self.db_path = Path(str(self.db_path)).expanduser().resolve()
        return self


# ---------------------------------------------------------------------------
# Top-level config
# ---------------------------------------------------------------------------


class GatewayConfig(BaseModel):
    """Root gateway configuration loaded from gateway.toml.

    All fields have sensible personal-tier defaults so the config file
    only needs to specify values that differ from the defaults.

    Usage::

        config = GatewayConfig.from_toml(Path("~/.arc/gateway.toml"))
        if config.gateway.tier == "federal":
            ...
    """

    gateway: GatewaySection = Field(default_factory=GatewaySection)
    security: SecuritySection = Field(default_factory=SecuritySection)
    platforms: PlatformsSection = Field(default_factory=PlatformsSection)
    pairing: PairingSection = Field(default_factory=PairingSection)

    @classmethod
    def load(cls) -> GatewayConfig:
        """Discover and load gateway config from the standard location.

        Reads ``${ARC_CONFIG_DIR:-~/.arc}/gateway.toml``. If absent, returns
        an all-defaults GatewayConfig (personal tier, no adapters enabled).

        Returns:
            Validated GatewayConfig instance.
        """
        import os

        base = os.environ.get("ARC_CONFIG_DIR")
        root = Path(base).expanduser() if base else Path.home() / ".arc"
        return cls.from_toml(root / "gateway.toml")

    @classmethod
    def from_toml(cls, path: Path) -> GatewayConfig:
        """Load config from a TOML file.

        Falls back to an all-defaults GatewayConfig if the file does not
        exist — useful for ``arc gateway start`` on a fresh install without
        a config file (personal tier, no adapters enabled).

        Args:
            path: Path to gateway.toml (expanded and resolved by caller).

        Returns:
            Validated GatewayConfig instance.

        Raises:
            ValueError: If the TOML is structurally invalid (parse error or
                Pydantic validation failure). Does NOT raise on missing file.
        """
        expanded = Path(str(path)).expanduser().resolve()
        if not expanded.exists():
            _logger.info(
                "GatewayConfig: no config file at %s; using defaults (personal tier)",
                expanded,
            )
            return cls()

        try:
            import tomllib  # stdlib Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-redef]
            except ImportError as exc:
                raise ImportError(
                    "tomllib (Python 3.11+) or tomli is required to load TOML config. "
                    "Install with: pip install tomli"
                ) from exc

        with open(expanded, "rb") as fh:
            raw = tomllib.load(fh)

        return cls.model_validate(raw)

    @classmethod
    def from_toml_str(cls, toml_text: str) -> GatewayConfig:
        """Parse config from a TOML string (for tests and setup wizard).

        Args:
            toml_text: TOML-formatted configuration text.

        Returns:
            Validated GatewayConfig instance.
        """
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib  # type: ignore[no-redef]

        raw = tomllib.loads(toml_text)
        return cls.model_validate(raw)

    def effective_agent_did(self, platform: str) -> str:
        """Return the agent DID for the given platform.

        Platform-level agent_did overrides the gateway-level default.
        Falls back to the [gateway].agent_did if the platform config
        has an empty agent_did.

        Args:
            platform: Platform name ("telegram", "slack", "mattermost", etc.).

        Returns:
            Agent DID string.
        """
        if platform == "telegram":
            plat_did = self.platforms.telegram.agent_did
        elif platform == "slack":
            plat_did = self.platforms.slack.agent_did
        elif platform == "mattermost":
            plat_did = self.platforms.mattermost.agent_did
        elif platform == "web":
            plat_did = self.platforms.web.agent_did
        else:
            plat_did = ""
        return plat_did or self.gateway.agent_did
