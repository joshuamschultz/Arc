"""CLI smoke tests for arcgateway.cli subcommands.

Tests cover:
- cmd_stop: no PID file → informational message, no crash
- cmd_stop: valid PID file → os.kill(pid, SIGTERM) called
- cmd_stop: dead PID → stale file removed, message printed
- cmd_stop: permission denied → error printed, no crash
- cmd_status: no files → reports not running
- cmd_status: PID file present → reports running
- cmd_status: clean-shutdown marker present → reports last shutdown time
- cmd_setup: creates gateway.toml with 0600 permissions
- cmd_setup: does NOT overwrite existing config
- main(): dispatches to correct subcommand handlers
- main(): no command → prints help, exits 1
- _wire_adapters: type validation
- _wire_adapters: Telegram and Slack platform paths (token present/absent, import error)
- cmd_start: calls asyncio.run with a coroutine
"""

from __future__ import annotations

import os
import signal
import stat
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from arcgateway.cli import (
    _wire_adapters,
    cmd_setup,
    cmd_start,
    cmd_status,
    cmd_stop,
    main,
)

# ---------------------------------------------------------------------------
# cmd_stop
# ---------------------------------------------------------------------------


def test_cmd_stop_no_pid_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_stop prints informational message when no PID file exists."""
    cmd_stop(runtime_dir=tmp_path)
    out = capsys.readouterr().out.lower()
    # The actual message is: "arcgateway stop: no PID file found at ..."
    assert "pid file" in out and ("no" in out or "not found" in out or "found" in out)


def test_cmd_stop_sends_sigterm(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_stop sends SIGTERM to the PID found in gateway.pid."""
    pid_file = tmp_path / "gateway.pid"
    fake_pid = 54321
    pid_file.write_text(f"{fake_pid}\n", encoding="utf-8")

    with patch("os.kill") as mock_kill:
        cmd_stop(runtime_dir=tmp_path)

    mock_kill.assert_called_once_with(fake_pid, signal.SIGTERM)
    out = capsys.readouterr().out
    assert str(fake_pid) in out


def test_cmd_stop_dead_pid_removes_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_stop removes stale PID file when process no longer exists."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("999999999\n", encoding="utf-8")

    with patch("os.kill", side_effect=ProcessLookupError):
        cmd_stop(runtime_dir=tmp_path)

    assert not pid_file.exists(), "Stale PID file must be removed"
    out = capsys.readouterr().out.lower()
    # Message says something about the process not being found.
    assert "stale" in out or "not found" in out or "exited" in out or "already" in out


def test_cmd_stop_permission_denied(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_stop prints error and does NOT raise on PermissionError."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("12345\n", encoding="utf-8")

    with patch("os.kill", side_effect=PermissionError("denied")):
        cmd_stop(runtime_dir=tmp_path)  # must not raise

    out = capsys.readouterr().out.lower()
    assert "permission" in out


def test_cmd_stop_corrupted_pid_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_stop handles non-integer PID file content gracefully."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("not-a-pid\n", encoding="utf-8")

    cmd_stop(runtime_dir=tmp_path)  # must not raise

    out = capsys.readouterr().out.lower()
    assert "could not read" in out or "pid" in out


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


def test_cmd_status_no_files(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_status reports no PID file and no clean-shutdown marker."""
    cmd_status(runtime_dir=tmp_path)
    out = capsys.readouterr().out.lower()
    # Should mention no PID file
    assert "pid file" in out
    # Should mention no clean-shutdown marker (running or crashed)
    assert "clean-shutdown" in out or "running" in out or "crashed" in out or "no clean" in out


def test_cmd_status_with_pid_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_status reports likely-running when PID file exists."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("99999\n", encoding="utf-8")

    cmd_status(runtime_dir=tmp_path)
    out = capsys.readouterr().out
    assert "99999" in out


def test_cmd_status_with_clean_marker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_status reports clean shutdown timestamp from marker file."""
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("2026-04-18T12:00:00+00:00\n", encoding="utf-8")

    cmd_status(runtime_dir=tmp_path)
    out = capsys.readouterr().out
    assert "2026-04-18" in out


def test_cmd_status_unreadable_pid_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_status handles unreadable PID file gracefully."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("12345\n", encoding="utf-8")

    with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
        cmd_status(runtime_dir=tmp_path)  # must not raise


def test_cmd_status_unreadable_marker(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_status handles unreadable .clean_shutdown file gracefully."""
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("2026-04-18T12:00:00+00:00\n", encoding="utf-8")

    original_read = Path.read_text

    def _patched_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == ".clean_shutdown":
            raise OSError("unreadable")
        return original_read(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "read_text", _patched_read):
        cmd_status(runtime_dir=tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# cmd_setup
# ---------------------------------------------------------------------------


def test_cmd_setup_creates_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_setup creates gateway.toml when it doesn't exist."""
    config_path = tmp_path / ".arc" / "gateway.toml"

    with patch("arcgateway.cli._DEFAULT_CONFIG", config_path):
        cmd_setup()

    assert config_path.exists(), "gateway.toml must be created"
    content = config_path.read_text(encoding="utf-8")
    assert "[gateway]" in content
    assert "tier" in content


def test_cmd_setup_sets_0600_permissions(tmp_path: Path) -> None:
    """cmd_setup creates gateway.toml with 0600 permissions."""
    config_path = tmp_path / ".arc" / "gateway.toml"

    with patch("arcgateway.cli._DEFAULT_CONFIG", config_path):
        cmd_setup()

    mode = stat.S_IMODE(config_path.stat().st_mode)
    assert mode == 0o600, f"Expected 0600 permissions, got {oct(mode)}"


def test_cmd_setup_does_not_overwrite_existing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_setup does NOT overwrite an existing gateway.toml."""
    config_path = tmp_path / ".arc" / "gateway.toml"
    config_path.parent.mkdir(parents=True)
    original_content = "existing config"
    config_path.write_text(original_content, encoding="utf-8")

    with patch("arcgateway.cli._DEFAULT_CONFIG", config_path):
        cmd_setup()

    assert config_path.read_text(encoding="utf-8") == original_content
    out = capsys.readouterr().out.lower()
    assert "already exists" in out or "config" in out


# ---------------------------------------------------------------------------
# cmd_start
# ---------------------------------------------------------------------------


def test_cmd_start_calls_asyncio_run(tmp_path: Path) -> None:
    """cmd_start calls asyncio.run() to start the daemon event loop."""
    toml_text = """
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")

    # Patch asyncio.run to capture the coroutine without actually running it.
    with patch("asyncio.run") as mock_run:
        cmd_start(config_path=config_file, runtime_dir=tmp_path)

    assert mock_run.called, "asyncio.run() must be called to start the daemon"


def test_cmd_start_missing_config_uses_defaults(tmp_path: Path) -> None:
    """cmd_start with a missing config file falls back to GatewayConfig defaults.

    GatewayConfig.from_toml() returns all-defaults when the file is missing
    (personal tier, no adapters enabled). This is intentional for fresh installs.
    """
    nonexistent = tmp_path / "missing.toml"
    assert not nonexistent.exists()

    # Patch asyncio.run to prevent the daemon from starting.
    with patch("asyncio.run") as mock_run:
        cmd_start(config_path=nonexistent)

    # asyncio.run must be called even with a missing config file.
    assert mock_run.called


def test_cmd_start_applies_runtime_dir_override(tmp_path: Path) -> None:
    """cmd_start uses the provided runtime_dir override (no daemon started)."""
    toml_text = """
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")
    custom_runtime = tmp_path / "custom_run"

    with patch("asyncio.run"):
        # Must not raise with a custom runtime_dir.
        cmd_start(config_path=config_file, runtime_dir=custom_runtime)


def test_cmd_start_personal_tier_no_adapters(tmp_path: Path) -> None:
    """cmd_start on personal tier with no platforms enabled completes cleanly."""
    toml_text = """
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"

[platforms.telegram]
enabled = false

[platforms.slack]
enabled = false
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")

    with patch("asyncio.run") as mock_run:
        cmd_start(config_path=config_file, runtime_dir=tmp_path)

    assert mock_run.called


# ---------------------------------------------------------------------------
# _wire_adapters: platform paths
# ---------------------------------------------------------------------------


def _make_real_runner_and_config(
    tmp_path: Path,
    *,
    tier: str = "personal",
    telegram_enabled: bool = False,
    slack_enabled: bool = False,
) -> tuple[object, object]:
    """Return a real GatewayRunner + GatewayConfig for _wire_adapters tests."""
    from arcgateway.config import GatewayConfig
    from arcgateway.runner import GatewayRunner

    toml = f"""
[gateway]
tier = "{tier}"
agent_did = "did:arc:agent:test"
runtime_dir = "{tmp_path}"

[platforms.telegram]
enabled = {"true" if telegram_enabled else "false"}
token_env = "TEST_TELEGRAM_TOKEN"

[platforms.slack]
enabled = {"true" if slack_enabled else "false"}
bot_token_env = "TEST_SLACK_BOT_TOKEN"
app_token_env = "TEST_SLACK_APP_TOKEN"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml, encoding="utf-8")
    config = GatewayConfig.from_toml(config_file)
    runner = GatewayRunner.from_config(config)
    # Attach a mock session_router so _wire_adapters can call runner.session_router.handle
    runner._session_router = MagicMock()  # type: ignore[assignment]
    return runner, config


def test_wire_adapters_telegram_missing_token_personal_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """_wire_adapters warns (not error) on missing Telegram token at personal tier."""
    runner, config = _make_real_runner_and_config(tmp_path, telegram_enabled=True)

    # Ensure env var is absent.
    env_without_token = {k: v for k, v in os.environ.items() if k != "TEST_TELEGRAM_TOKEN"}
    with patch.dict(os.environ, env_without_token, clear=True):
        _wire_adapters(runner, config)  # must not raise, must not sys.exit


def test_wire_adapters_telegram_present_token_registers_adapter(tmp_path: Path) -> None:
    """_wire_adapters registers TelegramAdapter when token is present."""
    runner, config = _make_real_runner_and_config(tmp_path, telegram_enabled=True)

    fake_adapter = MagicMock()
    fake_adapter.name = "telegram"

    with (
        patch.dict(os.environ, {"TEST_TELEGRAM_TOKEN": "fake-token"}),
        patch("arcgateway.adapters.telegram.TelegramAdapter", return_value=fake_adapter),
    ):
        _wire_adapters(runner, config)

    # runner should have the adapter registered.
    from arcgateway.runner import GatewayRunner

    assert isinstance(runner, GatewayRunner)
    # adapter was added.
    assert fake_adapter in runner._adapters


def test_wire_adapters_telegram_import_error_personal_skips(tmp_path: Path) -> None:
    """_wire_adapters skips Telegram adapter when import fails at personal tier."""
    runner, config = _make_real_runner_and_config(tmp_path, telegram_enabled=True)

    with (
        patch.dict(os.environ, {"TEST_TELEGRAM_TOKEN": "fake-token"}),
        patch(
            "arcgateway.adapters.telegram.TelegramAdapter",
            side_effect=ImportError("python-telegram-bot not installed"),
        ),
    ):
        # Must not raise at personal tier.
        _wire_adapters(runner, config)


def test_wire_adapters_telegram_missing_token_federal_exits(tmp_path: Path) -> None:
    """_wire_adapters hard-exits at federal tier when Telegram token is missing."""
    runner, config = _make_real_runner_and_config(tmp_path, tier="federal", telegram_enabled=True)

    env_without_token = {k: v for k, v in os.environ.items() if k != "TEST_TELEGRAM_TOKEN"}
    with (
        patch.dict(os.environ, env_without_token, clear=True),
        pytest.raises(SystemExit),
    ):
        _wire_adapters(runner, config)


def test_wire_adapters_slack_missing_token_personal_warns(tmp_path: Path) -> None:
    """_wire_adapters warns on missing Slack token at personal tier."""
    runner, config = _make_real_runner_and_config(tmp_path, slack_enabled=True)

    env_without = {
        k: v
        for k, v in os.environ.items()
        if k not in ("TEST_SLACK_BOT_TOKEN", "TEST_SLACK_APP_TOKEN")
    }
    with patch.dict(os.environ, env_without, clear=True):
        _wire_adapters(runner, config)  # must not raise


def test_wire_adapters_slack_present_tokens_registers_adapter(tmp_path: Path) -> None:
    """_wire_adapters registers SlackAdapter when both tokens are present."""
    runner, config = _make_real_runner_and_config(tmp_path, slack_enabled=True)

    fake_adapter = MagicMock()
    fake_adapter.name = "slack"

    with (
        patch.dict(
            os.environ,
            {"TEST_SLACK_BOT_TOKEN": "xoxb-fake", "TEST_SLACK_APP_TOKEN": "xapp-fake"},
        ),
        patch("arcgateway.adapters.slack.SlackAdapter", return_value=fake_adapter),
    ):
        _wire_adapters(runner, config)

    from arcgateway.runner import GatewayRunner

    assert isinstance(runner, GatewayRunner)
    assert fake_adapter in runner._adapters


def test_wire_adapters_slack_import_error_personal_skips(tmp_path: Path) -> None:
    """_wire_adapters skips Slack adapter when import fails at personal tier."""
    runner, config = _make_real_runner_and_config(tmp_path, slack_enabled=True)

    with (
        patch.dict(
            os.environ,
            {"TEST_SLACK_BOT_TOKEN": "xoxb-fake", "TEST_SLACK_APP_TOKEN": "xapp-fake"},
        ),
        patch(
            "arcgateway.adapters.slack.SlackAdapter",
            side_effect=ImportError("slack-bolt not installed"),
        ),
    ):
        _wire_adapters(runner, config)  # must not raise at personal tier


def test_wire_adapters_slack_missing_token_federal_exits(tmp_path: Path) -> None:
    """_wire_adapters hard-exits at federal tier when Slack token is missing."""
    runner, config = _make_real_runner_and_config(tmp_path, tier="federal", slack_enabled=True)

    env_without = {
        k: v
        for k, v in os.environ.items()
        if k not in ("TEST_SLACK_BOT_TOKEN", "TEST_SLACK_APP_TOKEN")
    }
    with (
        patch.dict(os.environ, env_without, clear=True),
        pytest.raises(SystemExit),
    ):
        _wire_adapters(runner, config)


# ---------------------------------------------------------------------------
# _wire_adapters type validation
# ---------------------------------------------------------------------------


def test_wire_adapters_rejects_non_runner() -> None:
    """_wire_adapters raises TypeError for non-GatewayRunner argument."""
    with pytest.raises(TypeError, match="GatewayRunner"):
        _wire_adapters("not a runner", MagicMock())


def test_wire_adapters_rejects_non_config() -> None:
    """_wire_adapters raises TypeError for non-GatewayConfig argument."""
    from arcgateway.runner import GatewayRunner

    fake_runner = MagicMock(spec=GatewayRunner)

    with pytest.raises(TypeError):
        _wire_adapters(fake_runner, "not a config")


# ---------------------------------------------------------------------------
# main() dispatcher
# ---------------------------------------------------------------------------


def test_main_no_command_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    """main() exits with code 1 when no subcommand is given."""
    with patch.object(sys, "argv", ["arcgateway"]):
        with pytest.raises(SystemExit) as exc_info:
            main()
    assert exc_info.value.code == 1


def test_main_dispatches_stop(tmp_path: Path) -> None:
    """main() with 'stop' subcommand calls cmd_stop."""
    with (
        patch.object(sys, "argv", ["arcgateway", "stop", "--runtime-dir", str(tmp_path)]),
        patch("arcgateway.cli.cmd_stop") as mock_stop,
    ):
        main()

    mock_stop.assert_called_once()


def test_main_dispatches_status(tmp_path: Path) -> None:
    """main() with 'status' subcommand calls cmd_status."""
    with (
        patch.object(sys, "argv", ["arcgateway", "status", "--runtime-dir", str(tmp_path)]),
        patch("arcgateway.cli.cmd_status") as mock_status,
    ):
        main()

    mock_status.assert_called_once()


def test_main_dispatches_setup() -> None:
    """main() with 'setup' subcommand calls cmd_setup."""
    with (
        patch.object(sys, "argv", ["arcgateway", "setup"]),
        patch("arcgateway.cli.cmd_setup") as mock_setup,
    ):
        main()

    mock_setup.assert_called_once()


def test_main_dispatches_start(tmp_path: Path) -> None:
    """main() with 'start' subcommand calls cmd_start."""
    with (
        patch.object(sys, "argv", ["arcgateway", "start"]),
        patch("arcgateway.cli.cmd_start") as mock_start,
    ):
        main()

    mock_start.assert_called_once()
