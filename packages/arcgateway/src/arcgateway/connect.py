"""Bind a Telegram bot to one agent — the shared core behind every connect surface.

Both ``arc gateway connect-telegram`` (arccli) and the arcui settings panel call
:func:`connect_telegram`. It stores the bot token in the env file the gateway reads
(0600, never the config) under a per-agent var, and writes a per-agent
``[platforms.<slug>_telegram]`` block to ``gateway.toml`` with ``platform = "telegram"``
so the fleet can run one bot per agent (see ``adapters/registry.py``). The token is a
credential: callers must never echo, log, or route it through an agent chat/LLM.

Lives in arcgateway (not arccli) because it mutates gateway config, and arcui — which
also needs it — depends on arcgateway, never on arccli.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import arcagent

# Telegram bot tokens: "<bot_id>:<secret>" — digits, colon, ~35 url-safe chars.
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
# Platform block names must satisfy the adapter-name guard (^[a-z][a-z0-9_]{0,31}$).
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def connect_telegram(
    *,
    agent_slug: str,
    agent_did: str,
    token: str,
    user_id: int,
    gateway_config: Path,
    env_file: Path,
) -> dict[str, str]:
    """Wire a Telegram bot to ``agent_did``. Returns ``{block, token_env, agent_did}``.

    Stores ``token`` in ``env_file`` under a per-agent var (0600) and adds/replaces a
    ``[platforms.<block>]`` telegram block in ``gateway_config`` bound to ``agent_did``
    and allowlisted to ``user_id``. Raises ``ValueError`` on a malformed token or DID —
    nothing is written in that case.
    """
    if not _TOKEN_RE.match(token.strip()):
        raise ValueError(
            "that does not look like a Telegram bot token (expected '<digits>:<letters>' "
            "from @BotFather). Nothing was written."
        )
    if not agent_did.strip():
        raise ValueError("no agent DID to bind the bot to. Nothing was written.")
    slug = _SLUG_RE.sub("_", agent_slug.lower()).strip("_") or "agent"
    block = f"{slug}_telegram"[:32]
    token_env = f"TELEGRAM_BOT_TOKEN_{slug.upper()}"

    _upsert_env(env_file, token_env, token.strip())
    _write_gateway_block(gateway_config, block, token_env, agent_did.strip(), user_id)
    return {"block": block, "token_env": token_env, "agent_did": agent_did.strip()}


def _upsert_env(env_file: Path, key: str, value: str) -> None:
    """Insert or replace ``KEY=value`` in the env file, keeping it owner-only (0600)."""
    env_file.parent.mkdir(parents=True, exist_ok=True)
    lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
    out: list[str] = []
    replaced = False
    for line in lines:
        if line.startswith(f"{key}="):
            out.append(f"{key}={value}")
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(f"{key}={value}")
    env_file.write_text("\n".join(out) + "\n", encoding="utf-8")
    env_file.chmod(0o600)


def _write_gateway_block(
    gateway_config: Path, block: str, token_env: str, agent_did: str, user_id: int
) -> None:
    """Add/replace the agent's telegram block in gateway.toml + ensure pairing is on."""
    data: dict[str, Any] = {}
    if gateway_config.is_file():
        data = tomllib.loads(gateway_config.read_text(encoding="utf-8"))
    platforms = data.setdefault("platforms", {})
    platforms[block] = {
        "enabled": True,
        "platform": "telegram",
        "token_env": token_env,
        "agent_did": agent_did,
        "allowed_user_ids": [user_id],
    }
    data.setdefault("security", {})["require_pairing"] = True
    gateway_config.parent.mkdir(parents=True, exist_ok=True)
    gateway_config.write_text(arcagent.dumps_toml(data), encoding="utf-8")


__all__ = ["connect_telegram"]
