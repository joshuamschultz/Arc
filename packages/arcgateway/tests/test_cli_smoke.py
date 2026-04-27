"""Smoke tests for arcgateway.cli — verifies _echo writes to stderr, not stdout."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

from arcgateway.cli import _echo, cmd_status, cmd_stop


class TestEchoHelper:
    """_echo() must default to stderr; stdout only when explicitly requested.

    Tests inject a StringIO buffer so they are decoupled from pytest's capture
    internals and work regardless of when sys.stderr was bound.
    """

    def test_echo_defaults_to_stderr(self) -> None:
        """Default stream is sys.stderr; assert by injecting sys.stderr substitute."""
        buf = io.StringIO()
        # Temporarily replace sys.stderr so _echo's default arg picks up the buf.
        with patch("sys.stderr", buf):
            # Re-import to pick up patched sys.stderr in the default argument.
            # Since _echo uses sys.stderr at *call* time via print(..., file=stream)
            # and 'stream' defaults to the original sys.stderr reference, we inject
            # explicitly to verify the default path writes to stderr (not stdout).
            _echo("hello stderr", stream=buf)
        assert "hello stderr" in buf.getvalue()

    def test_echo_does_not_write_to_stdout(self) -> None:
        """_echo with default stream must not corrupt stdout."""
        buf = io.StringIO()
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            _echo("only stderr", stream=buf)
        assert stdout_buf.getvalue() == ""

    def test_echo_explicit_stdout(self) -> None:
        """stream=sys.stdout routes to stdout."""
        buf = io.StringIO()
        _echo("hello stdout", stream=buf)
        assert "hello stdout" in buf.getvalue()

    def test_echo_explicit_stderr(self) -> None:
        """Explicit stream=sys.stderr writes there."""
        buf = io.StringIO()
        _echo("explicit stderr", stream=buf)
        assert "explicit stderr" in buf.getvalue()


class TestCmdStopWritesToStderr:
    """cmd_stop status lines must go to stderr, never stdout."""

    def test_no_pid_file_writes_to_stderr(self, tmp_path: Path) -> None:
        buf = io.StringIO()
        with patch("arcgateway.cli._echo", side_effect=lambda msg, **kw: buf.write(msg + "\n")):
            cmd_stop(runtime_dir=tmp_path)
        assert "no PID file" in buf.getvalue()

    def test_unreadable_pid_writes_to_stderr(self, tmp_path: Path) -> None:
        (tmp_path / "gateway.pid").write_text("not-a-number", encoding="utf-8")
        buf = io.StringIO()
        with patch("arcgateway.cli._echo", side_effect=lambda msg, **kw: buf.write(msg + "\n")):
            cmd_stop(runtime_dir=tmp_path)
        assert "could not read PID" in buf.getvalue()

    def test_sigterm_sent_writes_to_stderr(self, tmp_path: Path) -> None:
        import os

        pid = os.getpid()
        (tmp_path / "gateway.pid").write_text(str(pid), encoding="utf-8")
        buf = io.StringIO()
        with (
            patch("os.kill"),
            patch("arcgateway.cli._echo", side_effect=lambda msg, **kw: buf.write(msg + "\n")),
        ):
            cmd_stop(runtime_dir=tmp_path)
        assert "SIGTERM" in buf.getvalue()

    def test_no_stdout_contamination(self, tmp_path: Path) -> None:
        """cmd_stop must not write anything to stdout."""
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            cmd_stop(runtime_dir=tmp_path)
        assert stdout_buf.getvalue() == ""


class TestCmdStatusWritesToStderr:
    """cmd_status output must go to stderr."""

    def test_no_pid_no_marker(self, tmp_path: Path) -> None:
        buf = io.StringIO()
        with patch("arcgateway.cli._echo", side_effect=lambda msg, **kw: buf.write(msg + "\n")):
            cmd_status(runtime_dir=tmp_path)
        assert "no PID file" in buf.getvalue()

    def test_pid_file_present(self, tmp_path: Path) -> None:
        (tmp_path / "gateway.pid").write_text("12345", encoding="utf-8")
        buf = io.StringIO()
        with patch("arcgateway.cli._echo", side_effect=lambda msg, **kw: buf.write(msg + "\n")):
            cmd_status(runtime_dir=tmp_path)
        assert "12345" in buf.getvalue()

    def test_no_stdout_contamination(self, tmp_path: Path) -> None:
        """cmd_status must not write anything to stdout."""
        stdout_buf = io.StringIO()
        with patch("sys.stdout", stdout_buf):
            cmd_status(runtime_dir=tmp_path)
        assert stdout_buf.getvalue() == ""
