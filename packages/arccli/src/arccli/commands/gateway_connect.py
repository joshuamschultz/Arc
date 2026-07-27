"""``arc gateway connect-telegram`` — guided, non-technical Telegram pairing.

The turnkey entry point: a user pastes the bot token they got from @BotFather and
their Telegram user-ID, and this wires a Telegram bot bound to ONE agent. The wiring
core lives in :func:`arcgateway.connect.connect_telegram` (shared with the arcui
settings panel); this module only resolves the agent's DID, prompts for the secrets,
and reports what happened.

Security: the token is captured with a hidden prompt and handed straight to the core,
which writes it ONLY to the env file (0600) — never echoed, logged, placed in a config,
or routed through an agent chat/LLM. That is why this is a CLI/settings-form action, not
an arcui ``/slash`` command an agent would ingest as a message.
"""

from __future__ import annotations

import argparse
import getpass
import sys
import tomllib
from pathlib import Path

from arcgateway.connect import connect_telegram

from arccli.commands._shared import err
from arccli.commands._shared import write as _out


def _agent_did(agent_dir: Path) -> str:
    """Read the agent's DID from ``<agent_dir>/arcagent.toml`` (``[identity].did``)."""
    cfg = agent_dir / "arcagent.toml"
    if not cfg.is_file():
        raise ValueError(f"no arcagent.toml at {cfg} — is that an agent directory?")
    did = tomllib.loads(cfg.read_text(encoding="utf-8")).get("identity", {}).get("did", "")
    if not did:
        raise ValueError(f"{cfg} has no [identity].did — create the agent first.")
    return str(did)


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
            agent_slug=agent_dir.name,
            agent_did=_agent_did(agent_dir),
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


__all__ = ["gateway_connect_telegram_handler"]
