"""CLI entry point for arcgateway.

Provides ``arc gateway`` subcommands:
    start   — Start the gateway daemon with config from TOML
    stop    — Stop a running gateway daemon (sends SIGTERM to PID file)
    status  — Report gateway health (clean-shutdown marker + basic state)
    setup   — Write a starter gateway.toml for personal-tier configuration

Integration note:
    The full CLI wiring through the centralized arccli command registry
    (T1.1) is pending. These entry points are functional and can be invoked
    directly as ``arcgateway start|stop|status|setup`` or registered
    into arccli.commands.COMMAND_REGISTRY once T1.1 lands.

    At that point:
    1. Each function here becomes a CommandDef handler.
    2. gateway_only=True gates them from CLI-only contexts.
    3. resolve_command("gateway start") dispatches here.

TODO T1.1: Wire through arccli.commands.registry.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import IO

_logger = logging.getLogger("arcgateway.cli")

# Default config and runtime paths (personal tier)
_DEFAULT_CONFIG = Path("~/.arc/gateway.toml")
_DEFAULT_RUNTIME_DIR = Path("~/.arc/gateway/run")
_PID_FILE_NAME = "gateway.pid"


def _echo(msg: str, *, stream: IO[str] | None = None) -> None:
    """Write *msg* to *stream* (default: sys.stderr).

    Using stderr for operational messages keeps stdout clean for structured
    output (JSON, etc.). The stream parameter is injected in tests so they
    can capture output without patching sys.stderr globally.
    """
    out: IO[str] = stream if stream is not None else sys.stderr
    print(msg, file=out)  # intentional CLI output


def cmd_start(
    *,
    config_path: Path | None = None,
    runtime_dir: Path | None = None,
) -> None:
    """Start the gateway daemon.

    Loads GatewayConfig from the specified (or default) TOML path.
    Selects executor based on tier: personal/enterprise → AsyncioExecutor,
    federal → SubprocessExecutor.

    Platform adapters are built from the config:
      - [platforms.telegram] enabled=true → TelegramAdapter
      - [platforms.slack]    enabled=true → SlackAdapter

    Token credentials are read from the environment variable names specified
    in the config (never inlined in the config file).

    Federal-tier vault: If tier=federal and a platform credential env var
    is missing, gateway startup hard-fails with an error message rather than
    silently proceeding with no adapters (VaultUnreachable).

    Args:
        config_path: Path to gateway.toml. Defaults to ~/.arc/gateway.toml.
        runtime_dir: Path for PID file and .clean_shutdown marker.
    """
    from arcgateway.config import GatewayConfig
    from arcgateway.runner import GatewayRunner

    resolved_config_path = (config_path or _DEFAULT_CONFIG).expanduser().resolve()
    _logger.info("arcgateway start: loading config from %s", resolved_config_path)

    config = GatewayConfig.from_toml(resolved_config_path)

    if runtime_dir:
        config.gateway.runtime_dir = runtime_dir.expanduser().resolve()

    runner = GatewayRunner.from_config(config)

    # Wire platform adapters from config
    _wire_adapters(runner, config)

    _logger.info(
        "arcgateway: starting daemon (tier=%s adapters=%d)",
        config.gateway.tier,
        len(runner._adapters),
    )
    asyncio.run(runner.run())


def _wire_adapters(runner: object, config: object) -> None:
    """Build and register platform adapters from config.

    Reads credentials from environment variables. On federal tier, missing
    credentials are a hard error. On personal/enterprise, missing credentials
    skip that adapter with a warning.

    Args:
        runner: GatewayRunner instance.
        config: GatewayConfig instance.
    """
    from arcgateway.config import GatewayConfig
    from arcgateway.runner import GatewayRunner

    if not isinstance(runner, GatewayRunner):
        msg = f"Expected GatewayRunner, got {type(runner).__name__}"
        raise TypeError(msg)
    if not isinstance(config, GatewayConfig):
        msg = f"Expected GatewayConfig, got {type(config).__name__}"
        raise TypeError(msg)

    tier = config.gateway.tier

    # Telegram
    if config.platforms.telegram.enabled:
        token = config.platforms.telegram.resolve_token()
        if token is None:
            msg = (
                f"arcgateway: Telegram token not found in env var "
                f"{config.platforms.telegram.token_env!r}"
            )
            if tier == "federal":
                _logger.error("%s — hard error at federal tier", msg)
                sys.exit(1)
            else:
                _logger.warning("%s — skipping Telegram adapter", msg)
        else:
            try:
                from arcgateway.adapters.telegram import TelegramAdapter

                agent_did = config.effective_agent_did("telegram")
                adapter = TelegramAdapter(
                    bot_token=token,
                    allowed_user_ids=config.platforms.telegram.allowed_user_ids,
                    on_message=runner.session_router.handle,
                    agent_did=agent_did,
                )
                runner.add_adapter(adapter)
                _logger.info("arcgateway: Telegram adapter registered (agent_did=%s)", agent_did)
            except ImportError:
                _logger.error(
                    "arcgateway: python-telegram-bot not installed. "
                    "Install with: pip install 'arcgateway[telegram]'"
                )
                if tier == "federal":
                    sys.exit(1)

    # Slack
    if config.platforms.slack.enabled:
        bot_token = config.platforms.slack.resolve_bot_token()
        app_token = config.platforms.slack.resolve_app_token()
        if bot_token is None or app_token is None:
            missing = (
                config.platforms.slack.bot_token_env
                if bot_token is None
                else config.platforms.slack.app_token_env
            )
            msg = (
                f"arcgateway: Slack token not found in env var {missing!r}"
            )
            if tier == "federal":
                _logger.error("%s — hard error at federal tier", msg)
                sys.exit(1)
            else:
                _logger.warning("%s — skipping Slack adapter", msg)
        else:
            try:
                from arcgateway.adapters.slack import SlackAdapter

                agent_did = config.effective_agent_did("slack")
                slack_adapter = SlackAdapter(
                    bot_token=bot_token,
                    app_token=app_token,
                    allowed_user_ids=config.platforms.slack.allowed_user_ids,
                    on_message=runner.session_router.handle,
                )
                runner.add_adapter(slack_adapter)
                _logger.info("arcgateway: Slack adapter registered (agent_did=%s)", agent_did)
            except ImportError:
                _logger.error(
                    "arcgateway: slack-bolt not installed. "
                    "Install with: pip install 'arcgateway[slack]'"
                )
                if tier == "federal":
                    sys.exit(1)


def cmd_stop(*, runtime_dir: Path | None = None) -> None:
    """Stop a running gateway daemon.

    Reads the PID from ``<runtime_dir>/gateway.pid`` and sends SIGTERM.
    Logs an informational message if no PID file is found (process may
    have already exited cleanly).

    Args:
        runtime_dir: Path containing gateway.pid. Defaults to ~/.arc/gateway/run.
    """
    import signal as _signal

    rt = (runtime_dir or _DEFAULT_RUNTIME_DIR).expanduser().resolve()
    pid_file = rt / _PID_FILE_NAME

    if not pid_file.exists():
        _echo(
            "arcgateway stop: no PID file found at "
            f"{pid_file} — is the gateway running?"
        )
        return

    try:
        pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError) as exc:
        _echo(f"arcgateway stop: could not read PID from {pid_file}: {exc}")
        return

    try:
        os.kill(pid, _signal.SIGTERM)
        _echo(f"arcgateway stop: sent SIGTERM to PID {pid}")
    except ProcessLookupError:
        _echo(
            f"arcgateway stop: process {pid} not found — may have already exited. "
            f"Removing stale PID file."
        )
        pid_file.unlink(missing_ok=True)
    except PermissionError as exc:
        _echo(f"arcgateway stop: permission denied sending SIGTERM to {pid}: {exc}")


def cmd_status(*, runtime_dir: Path | None = None) -> None:
    """Report gateway health.

    Checks:
    - Clean-shutdown marker (present = last stop was clean).
    - PID file (present = process likely running, though we don't verify).

    Full adapter-health reporting via a Unix domain socket is a future
    enhancement (T1.5 socket IPC).

    Args:
        runtime_dir: Path to gateway runtime directory.
    """
    rt = (runtime_dir or _DEFAULT_RUNTIME_DIR).expanduser().resolve()
    clean_marker = rt / ".clean_shutdown"
    pid_file = rt / _PID_FILE_NAME

    if pid_file.exists():
        try:
            pid = pid_file.read_text(encoding="utf-8").strip()
            _echo(f"Gateway: PID file found (pid={pid}) — process likely running.")
        except OSError:
            _echo("Gateway: PID file found but unreadable.")
    else:
        _echo("Gateway: no PID file found.")

    if clean_marker.exists():
        try:
            content = clean_marker.read_text(encoding="utf-8").strip()
            _echo(f"Gateway: last clean shutdown at {content}")
        except OSError:
            _echo("Gateway: clean-shutdown marker found but unreadable.")
    else:
        _echo("Gateway: no clean-shutdown marker found (either running or crashed).")

    _echo("Note: full adapter health reporting not yet implemented (T1.5 socket IPC).")


def cmd_setup() -> None:
    """Write a starter gateway.toml for personal-tier configuration.

    Creates ~/.arc/gateway.toml with commented-out defaults so operators
    can fill in their platform tokens.  Does NOT overwrite an existing file.
    """
    config_path = _DEFAULT_CONFIG.expanduser().resolve()
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        _echo(f"arcgateway setup: config already exists at {config_path}")
        _echo("Delete it to regenerate, or edit it directly.")
        return

    starter_config = """\
[gateway]
tier = "personal"
agent_did = "did:arc:agent:default"
# runtime_dir = "~/.arc/gateway/run"

[security]
require_pairing = false

[platforms.telegram]
enabled = false
token_env = "TELEGRAM_BOT_TOKEN"
# allowed_user_ids = [123456789]  # Your Telegram user ID

[platforms.slack]
enabled = false
bot_token_env = "SLACK_BOT_TOKEN"
app_token_env = "SLACK_APP_TOKEN"
# allowed_user_ids = ["UABC123"]  # Your Slack user ID

[pairing]
# db_path = "~/.arc/gateway/pairing.db"
"""

    config_path.write_text(starter_config, encoding="utf-8")
    # Chmod 0600 — config may contain env var names; keep permissions tight.
    config_path.chmod(0o600)
    _echo(f"arcgateway setup: wrote starter config to {config_path}")
    _echo("Edit the file and set your platform tokens via environment variables.")


def main() -> None:
    """Main entry point for ``arcgateway`` CLI invocation.

    Dispatches to the appropriate subcommand handler based on sys.argv.
    """
    import argparse

    parser = argparse.ArgumentParser(
        prog="arcgateway",
        description="arcgateway — makes ArcAgents reachable from any chat platform",
    )
    subparsers = parser.add_subparsers(dest="command", help="Subcommand")

    # start
    start_parser = subparsers.add_parser("start", help="Start the gateway daemon")
    start_parser.add_argument("--config", type=Path, default=None, help="Path to gateway.toml")
    start_parser.add_argument("--runtime-dir", type=Path, default=None)

    # stop
    stop_parser = subparsers.add_parser("stop", help="Stop a running gateway daemon")
    stop_parser.add_argument("--runtime-dir", type=Path, default=None)

    # status
    status_parser = subparsers.add_parser("status", help="Report gateway health")
    status_parser.add_argument("--runtime-dir", type=Path, default=None)

    # setup
    subparsers.add_parser("setup", help="Write a starter gateway.toml (personal tier)")

    args = parser.parse_args()

    if args.command == "start":
        cmd_start(config_path=args.config, runtime_dir=args.runtime_dir)
    elif args.command == "stop":
        cmd_stop(runtime_dir=args.runtime_dir)
    elif args.command == "status":
        cmd_status(runtime_dir=args.runtime_dir)
    elif args.command == "setup":
        cmd_setup()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
