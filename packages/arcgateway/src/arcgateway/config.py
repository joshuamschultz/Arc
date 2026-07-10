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
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

_logger = logging.getLogger("arcgateway.config")


def _config_base_dir() -> Path:
    """Resolve the Arc config base: ``${ARC_CONFIG_DIR:-~/.arc}``.

    Shared by every default that lives under the Arc config tree
    (runtime_dir, pairing db_path) so an isolated ``ARC_CONFIG_DIR``
    deployment — the pattern already used by ``GatewayConfig.load()``,
    ``arccli.commands.identity``, and ``arctrust.trust_store`` — keeps ALL
    of the gateway's on-disk state together instead of leaking to the
    real ``~/.arc``. Evaluated fresh on every default-factory call (not a
    module-level constant) so it reflects the env var at model-construction
    time, not import time.
    """
    env = os.environ.get("ARC_CONFIG_DIR")
    return Path(env).expanduser() if env else Path.home() / ".arc"


# ---------------------------------------------------------------------------
# Section models
# ---------------------------------------------------------------------------


class GatewaySection(BaseModel):
    """[gateway] section."""

    tier: Literal["personal", "enterprise", "federal"] = "personal"
    agent_did: str = "did:arc:agent:default"
    runtime_dir: Path = Field(default_factory=lambda: _config_base_dir() / "gateway" / "run")

    @model_validator(mode="after")
    def _expand_paths(self) -> GatewaySection:
        self.runtime_dir = Path(str(self.runtime_dir)).expanduser().resolve()
        return self


class SecuritySection(BaseModel):
    """[security] section."""

    require_pairing: bool = False


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
    """[platforms] section.

    ``web`` is the only platform the gateway core knows about — it's the
    in-process browser-chat adapter arcui hosts, not a remote platform with
    a credential and an extension package.

    Every other ``[platforms.<name>]`` block (telegram, slack, mattermost, …)
    is captured generically via ``extra="allow"`` and handed, as a raw dict,
    to the matching adapter plugin (``arcgateway.adapters.registry``). The
    plugin validates the block against its own Pydantic model. This keeps the
    gateway core free of any platform-specific config schema.
    """

    model_config = ConfigDict(extra="allow")

    web: WebPlatformConfig = Field(default_factory=WebPlatformConfig)

    def remote_blocks(self) -> dict[str, dict[str, Any]]:
        """Return raw ``[platforms.<name>]`` blocks for every non-web platform.

        These are the blocks the adapter registry iterates: each dict-shaped
        extra field is a remote platform whose plugin owns its config schema.
        """
        extra = self.__pydantic_extra__ or {}
        return {name: block for name, block in extra.items() if isinstance(block, dict)}


class PairingSection(BaseModel):
    """[pairing] section."""

    db_path: Path = Field(default_factory=lambda: _config_base_dir() / "gateway" / "pairing.db")

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
        return cls.from_toml(_config_base_dir() / "gateway.toml")

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
                import tomli as tomllib  # type: ignore[no-redef]  # reason: Python <3.11 fallback — tomli is the same API as stdlib tomllib
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
            import tomli as tomllib  # type: ignore[no-redef]  # reason: Python <3.11 fallback — tomli is the same API as stdlib tomllib

        raw = tomllib.loads(toml_text)
        return cls.model_validate(raw)

    def effective_agent_did(self, platform: str) -> str:
        """Return the agent DID for the given platform.

        A platform-level ``agent_did`` overrides the gateway-level default;
        an empty/absent platform value falls back to ``[gateway].agent_did``.
        Works for both the core ``web`` adapter and any remote platform block,
        without the gateway knowing platform-specific schemas.

        Args:
            platform: Platform name ("web", "telegram", "slack", …).

        Returns:
            Agent DID string.
        """
        if platform == "web":
            plat_did = self.platforms.web.agent_did
        else:
            block = self.platforms.remote_blocks().get(platform, {})
            raw = block.get("agent_did", "")
            plat_did = raw if isinstance(raw, str) else ""
        return plat_did or self.gateway.agent_did
