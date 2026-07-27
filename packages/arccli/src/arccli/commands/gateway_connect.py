"""``arc gateway connect-telegram`` — guided, non-technical Telegram pairing.

The turnkey entry point: a user pastes the bot token they got from @BotFather and
their Telegram user-ID, and this wires a Telegram bot bound to ONE agent — the token
goes to the env file the gateway reads (0600, never the config), and a per-agent
``[platforms.<agent>_telegram]`` block is written to ``gateway.toml`` with
``platform = "telegram"`` so the fleet can run one bot per agent (multi-bot, see
``arcgateway.adapters.registry``).

Security: the token is captured with a hidden prompt and written ONLY to the env
file — never echoed, never logged, never placed in a config or (critically) routed
through an agent chat/LLM. That is why this is a CLI/settings-form action, not an
arcui ``/slash`` command an agent would ingest as a message.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
import tomllib
from pathlib import Path
from typing import Any

from arccli.blueprints import dumps_toml
from arccli.commands._shared import err
from arccli.commands._shared import write as _out

# Telegram bot tokens: "<bot_id>:<secret>" — digits, colon, ~35 url-safe chars.
_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
# Platform block names must satisfy arcgateway's name guard (^[a-z][a-z0-9_]{0,31}$).
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def connect_telegram(
    *,
    agent_dir: Path,
    token: str,
    user_id: int,
    gateway_config: Path,
    env_file: Path,
) -> dict[str, str]:
    """Wire a Telegram bot to the agent at ``agent_dir``. Returns what was written.

    Stores the token in ``env_file`` under a per-agent var (0600) and adds a
    ``[platforms.<block>]`` telegram block to ``gateway_config`` bound to the agent's
    DID and allowlisted to ``user_id``. The token never enters the config file.
    """
    if not _TOKEN_RE.match(token.strip()):
        raise ValueError(
            "that does not look like a Telegram bot token (expected '<digits>:<letters>' "
            "from @BotFather). Nothing was written."
        )
    did = _agent_did(agent_dir)
    slug = _SLUG_RE.sub("_", agent_dir.name.lower()).strip("_") or "agent"
    block = f"{slug}_telegram"[:32]
    token_env = f"TELEGRAM_BOT_TOKEN_{slug.upper()}"

    _upsert_env(env_file, token_env, token.strip())
    _write_gateway_block(gateway_config, block, token_env, did, user_id)
    return {"block": block, "token_env": token_env, "agent_did": did}


def _agent_did(agent_dir: Path) -> str:
    """Read the agent's DID from ``<agent_dir>/arcagent.toml`` (``[identity].did``)."""
    cfg = agent_dir / "arcagent.toml"
    if not cfg.is_file():
        raise ValueError(f"no arcagent.toml at {cfg} — is that an agent directory?")
    did = tomllib.loads(cfg.read_text(encoding="utf-8")).get("identity", {}).get("did", "")
    if not did:
        raise ValueError(f"{cfg} has no [identity].did — create the agent first.")
    return str(did)


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
    gateway_config.write_text(dumps_toml(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Interactive handler (arc gateway connect-telegram)
# ---------------------------------------------------------------------------


def gateway_connect_telegram_handler(args: list[str]) -> None:
    """Parse argv, prompt for the token/user-id if omitted, and wire the bot."""
    parser = argparse.ArgumentParser(prog="arc gateway connect-telegram", add_help=True)
    parser.add_argument("--agent", required=True, help="Path to the agent directory to connect.")
    parser.add_argument("--user-id", type=int, default=None, help="Your Telegram numeric user ID.")
    parser.add_argument("--token", default=None, help="Bot token (omit to be prompted securely).")
    parser.add_argument(
        "--gateway-config",
        default=str(Path("~/.arc/gateway.toml").expanduser()),
        help="Gateway config to update (default: ~/.arc/gateway.toml).",
    )
    parser.add_argument(
        "--env-file",
        default=str(Path("~/.arc/arc.env").expanduser()),
        help="Env file the gateway reads the token from (default: ~/.arc/arc.env).",
    )
    ns = parser.parse_args(args)

    agent_dir = Path(ns.agent).expanduser().resolve()
    token = ns.token or getpass.getpass(
        "Paste your Telegram bot token from @BotFather (hidden): "
    )
    user_id = ns.user_id if ns.user_id is not None else _prompt_user_id()

    try:
        result = connect_telegram(
            agent_dir=agent_dir,
            token=token,
            user_id=user_id,
            gateway_config=Path(ns.gateway_config).expanduser(),
            env_file=Path(ns.env_file).expanduser(),
        )
    except ValueError as exc:
        err(f"arc gateway connect-telegram: {exc}")
        sys.exit(1)

    _out(f"Connected Telegram to {agent_dir.name}.")
    _out(f"  bound to agent DID : {result['agent_did']}")
    _out(f"  token stored in    : {ns.env_file}  (as {result['token_env']}, 0600)")
    _out(f"  gateway block      : [platforms.{result['block']}]")
    _out("")
    _out("Last step — restart the gateway so the bot goes live:")
    _out("  systemctl --user restart arc.service")
    _out("Then message your bot on Telegram; you're pre-approved as the allowed user.")


def _prompt_user_id() -> int:
    """Prompt for the Telegram numeric user ID (message @userinfobot to find it)."""
    raw = input("Your Telegram numeric user ID (message @userinfobot to get it): ").strip()
    try:
        return int(raw)
    except ValueError:
        err("arc gateway connect-telegram: the user ID must be a number.")
        sys.exit(1)


__all__ = ["connect_telegram", "gateway_connect_telegram_handler"]
