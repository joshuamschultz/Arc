"""Tests for `arc ui tail` subcommand.

arc ui tail connects to a running UI server as a viewer and streams events
to stdout as JSONL. Supports --layer, --agent, --group filters.

These are unit-level tests exercising the tail logic directly without
subprocess overhead. Subprocess test for the full end-to-end is in
test_standalone_launch.py (ui_tail_subprocess).
"""

from __future__ import annotations

import argparse

import pytest
from arccli.commands.ui import _build_parser


class TestTailParserArgs:
    """Parser produces the correct Namespace for all flag combinations."""

    def test_tail_subcommand_registered(self) -> None:
        parser = _build_parser()
        parsed = parser.parse_args(["tail"])
        assert parsed.subcmd == "tail"

    def test_default_host_and_port(self) -> None:
        parser = _build_parser()
        parsed = parser.parse_args(["tail"])
        assert parsed.host == "127.0.0.1"
        assert parsed.port == 8420

    def test_layer_flag(self) -> None:
        parser = _build_parser()
        parsed = parser.parse_args(["tail", "--layer", "llm"])
        assert parsed.layer == "llm"

    def test_agent_flag(self) -> None:
        parser = _build_parser()
        parsed = parser.parse_args(["tail", "--agent", "did:arc:local:executor/abc"])
        assert parsed.agent == "did:arc:local:executor/abc"

    def test_group_flag(self) -> None:
        parser = _build_parser()
        parsed = parser.parse_args(["tail", "--group", "research-team"])
        assert parsed.group == "research-team"

    def test_viewer_token_flag(self) -> None:
        parser = _build_parser()
        parsed = parser.parse_args(["tail", "--viewer-token", "my-token"])
        assert parsed.viewer_token == "my-token"

    def test_all_flags_composable(self) -> None:
        parser = _build_parser()
        parsed = parser.parse_args([
            "tail",
            "--host", "10.0.0.1",
            "--port", "9000",
            "--layer", "llm",
            "--agent", "agent1",
            "--group", "team-alpha",
            "--viewer-token", "tok",
        ])
        assert parsed.host == "10.0.0.1"
        assert parsed.port == 9000
        assert parsed.layer == "llm"
        assert parsed.agent == "agent1"
        assert parsed.group == "team-alpha"
        assert parsed.viewer_token == "tok"


class TestTailLayerValidation:
    """Invalid layer values should produce a clear error."""

    def test_invalid_layer_exits_with_error(self, capsys: pytest.CaptureFixture[str]) -> None:
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["tail", "--layer", "invalid_layer"])


class TestTailSubscriptionMessage:
    """_tail builds the correct subscribe message based on flags."""

    async def test_no_filters_subscribes_to_all(self) -> None:
        """With no filters, _tail sends an empty subscribe (receive everything)."""
        from arccli.commands.ui import _build_subscribe_message

        args = argparse.Namespace(layer=None, agent=None, group=None)
        msg = _build_subscribe_message(args)
        assert msg["type"] == "subscribe"
        # No filters → no restrictions
        assert msg.get("layers") is None or msg.get("layers") == []
        assert msg.get("agents") is None or msg.get("agents") == []

    async def test_layer_filter_in_subscribe(self) -> None:
        from arccli.commands.ui import _build_subscribe_message

        args = argparse.Namespace(layer="llm", agent=None, group=None)
        msg = _build_subscribe_message(args)
        assert msg["type"] == "subscribe"
        assert "llm" in msg.get("layers", [])

    async def test_agent_filter_in_subscribe(self) -> None:
        from arccli.commands.ui import _build_subscribe_message

        args = argparse.Namespace(layer=None, agent="agent-xyz", group=None)
        msg = _build_subscribe_message(args)
        assert "agent-xyz" in msg.get("agents", [])

    async def test_group_filter_in_subscribe(self) -> None:
        from arccli.commands.ui import _build_subscribe_message

        args = argparse.Namespace(layer=None, agent=None, group="research-team")
        msg = _build_subscribe_message(args)
        assert "research-team" in msg.get("teams", [])

    async def test_composable_filters(self) -> None:
        from arccli.commands.ui import _build_subscribe_message

        args = argparse.Namespace(layer="llm", agent="agent1", group="team-alpha")
        msg = _build_subscribe_message(args)
        assert "llm" in msg.get("layers", [])
        assert "agent1" in msg.get("agents", [])
        assert "team-alpha" in msg.get("teams", [])


class TestTailHelpText:
    """arc ui tail --help documents all filter flags."""

    def test_tail_help_shows_layer_flag(self) -> None:
        import subprocess
        from pathlib import Path

        arc = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"
        if not arc.exists():
            pytest.skip("arc binary not found")

        result = subprocess.run(
            [str(arc), "ui", "tail", "--help"],
            capture_output=True, text=True,
        )
        assert result.returncode == 0
        assert "--layer" in result.stdout

    def test_tail_help_shows_agent_flag(self) -> None:
        import subprocess
        from pathlib import Path

        arc = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"
        if not arc.exists():
            pytest.skip("arc binary not found")

        result = subprocess.run(
            [str(arc), "ui", "tail", "--help"],
            capture_output=True, text=True,
        )
        assert "--agent" in result.stdout

    def test_tail_help_shows_group_flag(self) -> None:
        import subprocess
        from pathlib import Path

        arc = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"
        if not arc.exists():
            pytest.skip("arc binary not found")

        result = subprocess.run(
            [str(arc), "ui", "tail", "--help"],
            capture_output=True, text=True,
        )
        assert "--group" in result.stdout

    def test_tail_appears_in_ui_help(self) -> None:
        import subprocess
        from pathlib import Path

        arc = Path(__file__).parent.parent.parent.parent / ".venv" / "bin" / "arc"
        if not arc.exists():
            pytest.skip("arc binary not found")

        result = subprocess.run(
            [str(arc), "ui", "--help"],
            capture_output=True, text=True,
        )
        assert "tail" in result.stdout
