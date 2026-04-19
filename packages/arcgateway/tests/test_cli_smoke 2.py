"""Smoke tests for arcgateway.cli — verifies _echo writes to stderr, not stdout."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from arcgateway.cli import _echo, cmd_status, cmd_stop


class TestEchoHelper:
    """_echo() must default to stderr; stdout only when explicitly requested."""

    def test_echo_defaults_to_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        _echo("hello stderr")
        captured = capsys.readouterr()
        assert "hello stderr" in captured.err
        assert captured.out == ""

    def test_echo_explicit_stdout(self, capsys: pytest.CaptureFixture[str]) -> None:
        _echo("hello stdout", stream=sys.stdout)
        captured = capsys.readouterr()
        assert "hello stdout" in captured.out
        assert captured.err == ""

    def test_echo_explicit_stderr(self, capsys: pytest.CaptureFixture[str]) -> None:
        _echo("explicit stderr", stream=sys.stderr)
        captured = capsys.readouterr()
        assert "explicit stderr" in captured.err
        assert captured.out == ""


class TestCmdStopWritesToStderr:
    """cmd_stop status lines must go to stderr, never stdout."""

    def test_no_pid_file_writes_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cmd_stop(runtime_dir=tmp_path)
        captured = capsys.readouterr()
        assert "no PID file" in captured.err
        assert captured.out == ""

    def test_unreadable_pid_writes_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "gateway.pid").write_text("not-a-number", encoding="utf-8")
        cmd_stop(runtime_dir=tmp_path)
        captured = capsys.readouterr()
        assert captured.out == ""
        # should print some error about bad PID
        assert "could not read PID" in captured.err

    def test_sigterm_sent_writes_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        import os

        # Use current process PID — os.kill(pid, SIGTERM) would actually terminate
        # us, so mock it instead.
        pid = os.getpid()
        (tmp_path / "gateway.pid").write_text(str(pid), encoding="utf-8")

        with patch("os.kill"):
            cmd_stop(runtime_dir=tmp_path)

        captured = capsys.readouterr()
        assert "SIGTERM" in captured.err
        assert captured.out == ""


class TestCmdStatusWritesToStderr:
    """cmd_status output must go to stderr."""

    def test_no_pid_no_marker_writes_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cmd_status(runtime_dir=tmp_path)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no PID file" in captured.err

    def test_pid_file_present_writes_to_stderr(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        (tmp_path / "gateway.pid").write_text("12345", encoding="utf-8")
        cmd_status(runtime_dir=tmp_path)
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "12345" in captured.err
