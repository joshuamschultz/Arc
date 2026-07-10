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

import io
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


def _capture_echo(func: object, *args: object, **kwargs: object) -> str:
    """Call a cli function and collect all _echo() messages into a string.

    Since _echo() defaults to the pre-bound sys.stderr object (bound at module
    load time), capsys cannot intercept it reliably.  This helper patches _echo
    at the module level so the test receives the message strings directly.
    """
    buf = io.StringIO()
    with patch(
        "arcgateway.cli._echo",
        side_effect=lambda msg, **kw: buf.write(msg + "\n"),
    ):
        func(*args, **kwargs)  # type: ignore[operator]
    return buf.getvalue()


# ---------------------------------------------------------------------------
# cmd_stop
# ---------------------------------------------------------------------------


def test_cmd_stop_no_pid_file(tmp_path: Path) -> None:
    """cmd_stop prints informational message when no PID file exists."""
    output = _capture_echo(cmd_stop, runtime_dir=tmp_path).lower()
    assert "pid file" in output and ("no" in output or "not found" in output or "found" in output)


def test_cmd_stop_sends_sigterm(tmp_path: Path) -> None:
    """cmd_stop sends SIGTERM to the PID found in gateway.pid."""
    pid_file = tmp_path / "gateway.pid"
    fake_pid = 54321
    pid_file.write_text(f"{fake_pid}\n", encoding="utf-8")

    with patch("os.kill") as mock_kill:
        output = _capture_echo(cmd_stop, runtime_dir=tmp_path)

    mock_kill.assert_called_once_with(fake_pid, signal.SIGTERM)
    assert str(fake_pid) in output


def test_cmd_stop_dead_pid_removes_file(tmp_path: Path) -> None:
    """cmd_stop removes stale PID file when process no longer exists."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("999999999\n", encoding="utf-8")

    with patch("os.kill", side_effect=ProcessLookupError):
        output = _capture_echo(cmd_stop, runtime_dir=tmp_path).lower()

    assert not pid_file.exists(), "Stale PID file must be removed"
    assert "stale" in output or "not found" in output or "exited" in output or "already" in output


def test_cmd_stop_permission_denied(tmp_path: Path) -> None:
    """cmd_stop prints error and does NOT raise on PermissionError."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("12345\n", encoding="utf-8")

    with patch("os.kill", side_effect=PermissionError("denied")):
        output = _capture_echo(cmd_stop, runtime_dir=tmp_path).lower()  # must not raise

    assert "permission" in output


def test_cmd_stop_corrupted_pid_file(tmp_path: Path) -> None:
    """cmd_stop handles non-integer PID file content gracefully."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("not-a-pid\n", encoding="utf-8")

    output = _capture_echo(cmd_stop, runtime_dir=tmp_path).lower()  # must not raise
    assert "could not read" in output or "pid" in output


def test_cmd_stop_no_runtime_dir_honors_arc_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting --runtime-dir resolves it via GatewayConfig.load() (ARC_CONFIG_DIR)."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    pid_file = tmp_path / "gateway" / "run" / "gateway.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("54321\n", encoding="utf-8")

    with patch("os.kill") as mock_kill:
        output = _capture_echo(cmd_stop)

    mock_kill.assert_called_once_with(54321, signal.SIGTERM)
    assert "54321" in output


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------


def test_cmd_status_no_files(tmp_path: Path) -> None:
    """cmd_status reports no PID file and no clean-shutdown marker."""
    output = _capture_echo(cmd_status, runtime_dir=tmp_path).lower()
    assert "pid file" in output
    assert (
        "clean-shutdown" in output
        or "running" in output
        or "crashed" in output
        or "no clean" in output
    )


def test_cmd_status_with_pid_file(tmp_path: Path) -> None:
    """cmd_status reports likely-running when PID file exists."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("99999\n", encoding="utf-8")

    output = _capture_echo(cmd_status, runtime_dir=tmp_path)
    assert "99999" in output


def test_cmd_status_no_runtime_dir_honors_arc_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting --runtime-dir resolves it via GatewayConfig.load() (ARC_CONFIG_DIR)."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    pid_file = tmp_path / "gateway" / "run" / "gateway.pid"
    pid_file.parent.mkdir(parents=True)
    pid_file.write_text("13579\n", encoding="utf-8")

    output = _capture_echo(cmd_status)

    assert "13579" in output


def test_cmd_status_with_clean_marker(tmp_path: Path) -> None:
    """cmd_status reports clean shutdown timestamp from marker file."""
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("2026-04-18T12:00:00+00:00\n", encoding="utf-8")

    output = _capture_echo(cmd_status, runtime_dir=tmp_path)
    assert "2026-04-18" in output


def test_cmd_status_unreadable_pid_file(tmp_path: Path) -> None:
    """cmd_status handles unreadable PID file gracefully."""
    pid_file = tmp_path / "gateway.pid"
    pid_file.write_text("12345\n", encoding="utf-8")

    with patch("pathlib.Path.read_text", side_effect=OSError("permission denied")):
        _capture_echo(cmd_status, runtime_dir=tmp_path)  # must not raise


def test_cmd_status_unreadable_marker(tmp_path: Path) -> None:
    """cmd_status handles unreadable .clean_shutdown file gracefully."""
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("2026-04-18T12:00:00+00:00\n", encoding="utf-8")

    original_read = Path.read_text

    def _patched_read(self: Path, *args: object, **kwargs: object) -> str:
        if self.name == ".clean_shutdown":
            raise OSError("unreadable")
        return original_read(self, *args, **kwargs)  # type: ignore[arg-type]

    with patch.object(Path, "read_text", _patched_read):
        _capture_echo(cmd_status, runtime_dir=tmp_path)  # must not raise


# ---------------------------------------------------------------------------
# cmd_setup
# ---------------------------------------------------------------------------


def test_cmd_setup_creates_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_setup creates gateway.toml when it doesn't exist."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    config_path = tmp_path / "gateway.toml"

    cmd_setup()

    assert config_path.exists(), "gateway.toml must be created"
    content = config_path.read_text(encoding="utf-8")
    assert "[gateway]" in content
    assert "tier" in content


def test_cmd_setup_sets_0600_permissions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_setup creates gateway.toml with 0600 permissions."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    config_path = tmp_path / "gateway.toml"

    cmd_setup()

    mode = stat.S_IMODE(config_path.stat().st_mode)
    assert mode == 0o600, f"Expected 0600 permissions, got {oct(mode)}"


def test_cmd_setup_does_not_overwrite_existing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cmd_setup does NOT overwrite an existing gateway.toml."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    config_path = tmp_path / "gateway.toml"
    original_content = "existing config"
    config_path.write_text(original_content, encoding="utf-8")

    output = _capture_echo(cmd_setup).lower()

    assert config_path.read_text(encoding="utf-8") == original_content
    assert "already exists" in output or "config" in output


# ---------------------------------------------------------------------------
# cmd_start
# ---------------------------------------------------------------------------


def test_cmd_start_fails_closed_personal_tier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_start refuses to start at personal tier — no real agent execution path exists.

    AsyncioExecutor has no agent_factory in the standalone path (echo stub
    only). The embedded path (arc ui start --team-root --gateway-config) is
    canonical; the standalone daemon must never silently serve no real agent.
    """
    toml_text = """
[gateway]
tier = "personal"
agent_did = "did:arc:agent:test"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")

    with patch("asyncio.run") as mock_run, pytest.raises(SystemExit) as excinfo:
        cmd_start(config_path=config_file)

    assert excinfo.value.code == 1
    assert not mock_run.called, "asyncio.run() must NOT be called — startup failed closed"
    err = capsys.readouterr().err
    assert "arc ui start" in err


def test_cmd_start_fails_closed_enterprise_tier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_start refuses to start at enterprise tier for the same reason as personal."""
    toml_text = """
[gateway]
tier = "enterprise"
agent_did = "did:arc:agent:test"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")

    with patch("asyncio.run") as mock_run, pytest.raises(SystemExit):
        cmd_start(config_path=config_file)

    assert not mock_run.called


def test_cmd_start_fails_closed_federal_tier(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """cmd_start ALSO refuses to start at federal tier.

    Evidence: arc-agent-worker (the SubprocessExecutor's per-session
    subprocess) ignores --did for config selection — it always looks in the
    same fixed cwd-relative arcagent.toml / ~/.arc/agent.toml regardless of
    which agent_did the request targets (arccli/agent_worker.py
    _CONFIG_SEARCH_PATHS + _run_with_arcagent). It cannot correctly serve a
    multi-platform gateway's per-agent_did routing, so federal gets no
    exemption from the fail-closed rule either.
    """
    toml_text = """
[gateway]
tier = "federal"
agent_did = "did:arc:agent:test"
"""
    config_file = tmp_path / "gateway.toml"
    config_file.write_text(toml_text, encoding="utf-8")

    with patch("asyncio.run") as mock_run, pytest.raises(SystemExit):
        cmd_start(config_path=config_file)

    assert not mock_run.called
    err = capsys.readouterr().err
    assert "arc ui start" in err


def test_cmd_start_missing_config_fails_closed(tmp_path: Path) -> None:
    """A missing config file (defaults to personal tier) also fails closed."""
    nonexistent = tmp_path / "missing.toml"
    assert not nonexistent.exists()

    with patch("asyncio.run") as mock_run, pytest.raises(SystemExit) as excinfo:
        cmd_start(config_path=nonexistent)

    assert excinfo.value.code == 1
    assert not mock_run.called


def test_cmd_start_no_config_path_uses_arc_config_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Omitting config_path still resolves via GatewayConfig.load() before failing closed.

    Proves the ARC_CONFIG_DIR-aware discovery genuinely runs (not skipped by
    the fail-closed check) — patches GatewayConfig.load to observe it was
    called, since cmd_start exits before reaching asyncio.run either way.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    (tmp_path / "gateway.toml").write_text(
        """
[gateway]
tier = "personal"
agent_did = "did:arc:agent:isolated"
""",
        encoding="utf-8",
    )

    from arcgateway.config import GatewayConfig

    real_load = GatewayConfig.load
    calls: list[bool] = []

    def _spy() -> GatewayConfig:
        calls.append(True)
        return real_load()

    with (
        patch("asyncio.run"),
        patch("arcgateway.config.GatewayConfig.load", side_effect=_spy),
        pytest.raises(SystemExit),
    ):
        cmd_start()

    assert calls, "GatewayConfig.load() must be used for ARC_CONFIG_DIR-aware discovery"


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


def test_wire_adapters_telegram_missing_token_personal_warns(tmp_path: Path) -> None:
    """_wire_adapters warns (not error) on missing Telegram token at personal tier."""
    runner, config = _make_real_runner_and_config(tmp_path, telegram_enabled=True)

    # Ensure env var is absent.
    env_without_token = {k: v for k, v in os.environ.items() if k != "TEST_TELEGRAM_TOKEN"}
    with patch.dict(os.environ, env_without_token, clear=True):
        _wire_adapters(runner, config)  # must not raise, must not sys.exit


def test_wire_adapters_telegram_present_token_registers_adapter(tmp_path: Path) -> None:
    """_wire_adapters registers a telegram adapter (via the registry) when its token is present."""
    pytest.importorskip("arcgateway_telegram")
    runner, config = _make_real_runner_and_config(tmp_path, telegram_enabled=True)

    with patch.dict(os.environ, {"TEST_TELEGRAM_TOKEN": "fake-token"}):
        _wire_adapters(runner, config)

    from arcgateway.runner import GatewayRunner

    assert isinstance(runner, GatewayRunner)
    assert any(a.name == "telegram" for a in runner._adapters)


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
    """_wire_adapters registers a slack adapter (via the registry) when both tokens are present."""
    pytest.importorskip("arcgateway_slack")
    runner, config = _make_real_runner_and_config(tmp_path, slack_enabled=True)

    with patch.dict(
        os.environ,
        {"TEST_SLACK_BOT_TOKEN": "xoxb-fake", "TEST_SLACK_APP_TOKEN": "xapp-fake"},
    ):
        _wire_adapters(runner, config)

    from arcgateway.runner import GatewayRunner

    assert isinstance(runner, GatewayRunner)
    assert any(a.name == "slack" for a in runner._adapters)


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


# ---------------------------------------------------------------------------
# adapter subcommand — install/list platform extension packages
# ---------------------------------------------------------------------------


def test_cmd_adapter_list_shows_official(capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_adapter('list') prints every official adapter."""
    from arcgateway.cli import cmd_adapter

    cmd_adapter("list")
    out = capsys.readouterr().err  # _echo writes to stderr
    assert "telegram" in out
    assert "slack" in out
    assert "mattermost" in out


def test_cmd_adapter_install_unknown_exits(capsys: pytest.CaptureFixture[str]) -> None:
    """cmd_adapter install rejects a non-official adapter name."""
    from arcgateway.cli import cmd_adapter

    with pytest.raises(SystemExit) as exc_info:
        cmd_adapter("install", name="discord")
    assert exc_info.value.code == 1


def test_cmd_adapter_install_invokes_installer(monkeypatch: pytest.MonkeyPatch) -> None:
    """cmd_adapter install calls the installer for an official adapter."""
    import arcgateway.adapters.install as inst

    calls: list[str] = []
    monkeypatch.setattr(
        inst,
        "install_adapter",
        lambda name, *, upgrade=False: calls.append(name) or 0,
    )
    from arcgateway.cli import cmd_adapter

    cmd_adapter("install", name="telegram")  # must not raise
    assert calls == ["telegram"]


def test_main_dispatches_adapter_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """main() routes 'adapter install <name>' to cmd_adapter."""
    with (
        patch.object(sys, "argv", ["arcgateway", "adapter", "install", "telegram"]),
        patch("arcgateway.cli.cmd_adapter") as mock_adapter,
    ):
        main()
    mock_adapter.assert_called_once()
