"""Interactive Telegram bot setup for an ArcAgent.

Guides the user through:
1. Bot token entry (with BotFather instructions)
2. Token verification via Telegram ``getMe()``
3. Chat ID auto-discovery via ``getUpdates`` polling
4. Token storage in ``~/.arc/.env``
5. ``arcagent.toml`` config update with ``[modules.telegram]``

Uses ``httpx`` directly — no ``python-telegram-bot`` dependency at setup time.
"""

from __future__ import annotations

import os
import time
import tomllib
from pathlib import Path

import click
import httpx

from arccli.formatting import click_echo

_TELEGRAM_API = "https://api.telegram.org/bot{token}"
_ENV_PATH = Path.home() / ".arc" / ".env"
_TOKEN_ENV_VAR = "ARCAGENT_TELEGRAM_BOT_TOKEN"  # noqa: S105 — env var name, not a secret
_HTTP_TIMEOUT = 10.0


# ── Public command ───────────────────────────────────────────────


@click.command("setup-telegram")
@click.argument("path", default=".")
def setup_telegram(path: str) -> None:
    """Interactive Telegram bot setup for an agent.

    Walks through bot token entry, verification, chat ID discovery,
    and config file updates.

    \b
    Examples:
      arc agent setup-telegram my-agent
      arc agent setup-telegram .
    """
    from arccli.agent import _resolve_agent_dir

    agent_dir = _resolve_agent_dir(path)
    config_path = agent_dir / "arcagent.toml"
    if not config_path.exists():
        raise click.ClickException(f"No arcagent.toml in {agent_dir}")

    click_echo("Telegram Bot Setup")
    click_echo("=" * 40)

    # Step 1: Get and verify bot token
    token, bot_username = _prompt_bot_token()

    # Step 2: Store token in .env
    _store_token(token)

    # Step 3: Discover chat_id
    chat_id = _discover_chat_id(token, bot_username)

    # Step 4: Update arcagent.toml
    _update_agent_config(config_path, chat_id)

    # Summary
    click_echo()
    click_echo("=" * 40)
    click_echo("Setup complete!")
    click_echo()
    click_echo(f"  Bot:       @{bot_username}")
    click_echo(f"  Chat ID:   {chat_id}")
    click_echo(f"  Token:     {_ENV_PATH}")
    click_echo(f"  Config:    {config_path}")
    click_echo()
    click_echo("Start chatting via Telegram:")
    click_echo(f"  arc agent serve {agent_dir}")


# ── Step helpers ─────────────────────────────────────────────────


def _prompt_bot_token() -> tuple[str, str]:
    """Prompt for bot token and verify it via ``getMe()``.

    Returns:
        Tuple of (token, bot_username).
    """
    click_echo()
    click_echo("Step 1: Bot Token")
    click_echo("-" * 40)
    click_echo("Create a bot via @BotFather on Telegram:")
    click_echo("  1. Open Telegram and search for @BotFather")
    click_echo("  2. Send /newbot and follow the prompts")
    click_echo("  3. Copy the bot token (looks like 123456:ABC-DEF...)")
    click_echo()

    while True:
        token = click.prompt("Bot token").strip()
        if not token:
            click_echo("  Token cannot be empty.")
            continue

        username = _verify_token(token)
        if username is not None:
            click_echo(f"  Verified: @{username}")
            return token, username

        click_echo("  Invalid token. Please check and try again.")


def _verify_token(token: str) -> str | None:
    """Call ``getMe()`` to verify the bot token.

    Returns:
        Bot username on success, ``None`` on failure.
    """
    url = f"{_TELEGRAM_API.format(token=token)}/getMe"
    try:
        resp = httpx.get(url, timeout=_HTTP_TIMEOUT)
        data = resp.json()
        if data.get("ok"):
            return data["result"]["username"]
    except (httpx.HTTPError, KeyError, ValueError):
        pass
    return None


def _discover_chat_id(token: str, bot_username: str) -> int:
    """Poll ``getUpdates`` to auto-discover the user's chat ID.

    Clears stale updates first, then waits for the user to send
    a message to the bot.

    Returns:
        The chat ID from the first received message.
    """
    click_echo()
    click_echo("Step 2: Chat ID Discovery")
    click_echo("-" * 40)
    click_echo(f"Send any message to @{bot_username} on Telegram.")
    click_echo("Waiting for your message...")
    click_echo()

    base = _TELEGRAM_API.format(token=token)

    # Clear stale updates so we only see new messages
    try:
        resp = httpx.get(
            f"{base}/getUpdates",
            params={"offset": -1, "timeout": 0},
            timeout=_HTTP_TIMEOUT,
        )
        data = resp.json()
        # If there was a stale update, set offset past it
        results = data.get("result", [])
        offset = results[-1]["update_id"] + 1 if results else 0
    except (httpx.HTTPError, KeyError, ValueError, IndexError):
        offset = 0

    # Poll for new messages (up to 120 seconds)
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(
                f"{base}/getUpdates",
                params={"offset": offset, "timeout": 5},
                timeout=_HTTP_TIMEOUT + 5,
            )
            data = resp.json()
            results = data.get("result", [])
            for update in results:
                message = update.get("message", {})
                chat = message.get("chat", {})
                chat_id = chat.get("id")
                if chat_id is not None:
                    sender = chat.get("first_name", "Unknown")
                    click_echo(f"  Found chat_id: {chat_id} ({sender})")
                    return int(chat_id)
        except (httpx.HTTPError, KeyError, ValueError):
            pass

        time.sleep(1)

    raise click.ClickException(
        "Timed out waiting for a message. "
        f"Make sure you sent a message to @{bot_username}."
    )


def _store_token(token: str) -> None:
    """Append the bot token to ``~/.arc/.env`` with ``0o600`` permissions.

    Skips if the token is already stored with the same value.
    """
    click_echo()
    click_echo("Step 3: Storing Token")
    click_echo("-" * 40)

    env_path = _ENV_PATH
    env_path.parent.mkdir(parents=True, exist_ok=True)

    line = f"{_TOKEN_ENV_VAR}={token}"

    # Check if already present
    if env_path.exists():
        existing = env_path.read_text()
        for existing_line in existing.splitlines():
            stripped = existing_line.strip()
            if stripped.startswith(f"{_TOKEN_ENV_VAR}="):
                if stripped == line:
                    click_echo(f"  Already stored in {env_path}")
                    return
                # Replace existing value
                new_content = existing.replace(existing_line, line)
                _write_file_secure(env_path, new_content)
                click_echo(f"  Updated in {env_path}")
                return

        # Append to existing file
        content = existing.rstrip("\n") + f"\n{line}\n"
        _write_file_secure(env_path, content)
    else:
        _write_file_secure(env_path, f"{line}\n")

    click_echo(f"  Saved to {env_path}")


def _write_file_secure(path: Path, content: str) -> None:
    """Write a file with owner-only permissions (``0o600``)."""
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode())
    finally:
        os.close(fd)


def _update_agent_config(config_path: Path, chat_id: int) -> None:
    """Add or update ``[modules.telegram]`` section in ``arcagent.toml``.

    If the section doesn't exist, appends it.
    If it exists, adds ``chat_id`` to ``allowed_chat_ids`` if not present.
    """
    click_echo()
    click_echo("Step 4: Updating Config")
    click_echo("-" * 40)

    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    text = config_path.read_text()

    telegram_conf = config.get("modules", {}).get("telegram", {})
    telegram_config = telegram_conf.get("config", {})

    if not telegram_conf:
        # No telegram section — append it
        section = (
            "\n[modules.telegram]\n"
            "enabled = true\n"
            "\n"
            "[modules.telegram.config]\n"
            f"allowed_chat_ids = [{chat_id}]\n"
            "poll_interval = 1.0\n"
        )
        text = text.rstrip("\n") + "\n" + section
        config_path.write_text(text)
        click_echo(f"  Added [modules.telegram] to {config_path.name}")
        return

    # Section exists — check allowed_chat_ids
    existing_ids: list[int] = telegram_config.get("allowed_chat_ids", [])
    if chat_id in existing_ids:
        click_echo(f"  Chat ID {chat_id} already in allowed_chat_ids")
        return

    # Add chat_id to existing list
    existing_ids.append(chat_id)
    new_list = "[" + ", ".join(str(cid) for cid in existing_ids) + "]"

    # Replace the allowed_chat_ids line in the file text
    import re

    pattern = r"allowed_chat_ids\s*=\s*\[.*?\]"
    if re.search(pattern, text):
        text = re.sub(pattern, f"allowed_chat_ids = {new_list}", text)
        config_path.write_text(text)
        click_echo(f"  Added chat_id {chat_id} to allowed_chat_ids")
    else:
        # allowed_chat_ids key doesn't exist in text but section does — append
        text = text.rstrip("\n") + f"\nallowed_chat_ids = {new_list}\n"
        config_path.write_text(text)
        click_echo(f"  Added allowed_chat_ids = {new_list}")
