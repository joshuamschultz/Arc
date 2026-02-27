"""E2E tests for arcteam.cli — full CLI command flows."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def run_cli(root: Path, *args: str) -> subprocess.CompletedProcess:  # type: ignore[type-arg]
    """Run arc-team CLI with given arguments."""
    cmd = [sys.executable, "-m", "arcteam.cli", "--root", str(root), *args]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10)


class TestFullFlow:
    """E2E: register -> channel -> join -> send -> inbox flow."""

    def test_full_messaging_flow(self, tmp_path: Path) -> None:
        # Register agents
        r = run_cli(
            tmp_path,
            "register",
            "agent://a1",
            "--name",
            "Agent One",
            "--type",
            "agent",
            "--roles",
            "ops",
        )
        assert r.returncode == 0
        assert "Registered" in r.stdout

        r = run_cli(
            tmp_path,
            "register",
            "agent://a2",
            "--name",
            "Agent Two",
            "--type",
            "agent",
            "--roles",
            "ops",
        )
        assert r.returncode == 0

        r = run_cli(
            tmp_path,
            "register",
            "user://josh",
            "--name",
            "Josh",
            "--type",
            "user",
            "--roles",
            "admin",
        )
        assert r.returncode == 0

        # Create channel with members
        r = run_cli(
            tmp_path,
            "channel",
            "ops-channel",
            "--members",
            "agent://a1,agent://a2",
            "--description",
            "Ops",
        )
        assert r.returncode == 0
        assert "Channel created" in r.stdout

        # List channels
        r = run_cli(tmp_path, "channels")
        assert r.returncode == 0
        assert "ops-channel" in r.stdout

        # List entities
        r = run_cli(tmp_path, "entities")
        assert r.returncode == 0
        assert "agent://a1" in r.stdout

        # Send message to channel
        r = run_cli(
            tmp_path,
            "--as",
            "agent://a1",
            "send",
            "--to",
            "channel://ops-channel",
            "--body",
            "Hello ops!",
        )
        assert r.returncode == 0
        assert "Sent:" in r.stdout

        # Check inbox for a2
        r = run_cli(tmp_path, "--as", "agent://a2", "inbox")
        assert r.returncode == 0
        assert "Hello ops!" in r.stdout


class TestDMFlow:
    """E2E: send DM -> read -> thread."""

    def test_dm_and_thread(self, tmp_path: Path) -> None:
        # Register
        run_cli(tmp_path, "register", "agent://a1", "--name", "A1", "--type", "agent")
        run_cli(tmp_path, "register", "user://josh", "--name", "Josh", "--type", "user")

        # Send DM
        r = run_cli(
            tmp_path, "--as", "user://josh", "send", "--to", "agent://a1", "--body", "Hey agent!"
        )
        assert r.returncode == 0
        r.stdout.strip().split("Sent: ")[1].split(" ")[0]

        # Read DM
        r = run_cli(tmp_path, "--as", "agent://a1", "read", "--dm", "a1")
        assert r.returncode == 0
        assert "Hey agent!" in r.stdout


class TestRoleFlow:
    """E2E: send to role -> inbox shows role messages."""

    def test_role_messaging(self, tmp_path: Path) -> None:
        run_cli(
            tmp_path, "register", "agent://a1", "--name", "A1", "--type", "agent", "--roles", "ops"
        )
        run_cli(tmp_path, "register", "user://josh", "--name", "Josh", "--type", "user")

        # Send to role
        r = run_cli(
            tmp_path,
            "--as",
            "user://josh",
            "send",
            "--to",
            "role://ops",
            "--body",
            "All ops check in",
        )
        assert r.returncode == 0

        # Check inbox for a1 (has ops role)
        r = run_cli(tmp_path, "--as", "agent://a1", "inbox")
        assert r.returncode == 0
        assert "All ops check in" in r.stdout


class TestJSONOutput:
    """--json flag outputs valid JSON."""

    def test_json_entities(self, tmp_path: Path) -> None:
        run_cli(tmp_path, "register", "agent://a1", "--name", "A1", "--type", "agent")
        r = run_cli(tmp_path, "--json", "entities")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["id"] == "agent://a1"

    def test_json_channels(self, tmp_path: Path) -> None:
        run_cli(tmp_path, "register", "agent://a1", "--name", "A1", "--type", "agent")
        run_cli(tmp_path, "channel", "test-ch", "--members", "agent://a1")
        r = run_cli(tmp_path, "--json", "channels")
        assert r.returncode == 0
        data = json.loads(r.stdout)
        assert isinstance(data, list)


class TestErrorHandling:
    """Invalid arguments produce helpful error messages."""

    def test_missing_required_args(self, tmp_path: Path) -> None:
        r = run_cli(tmp_path, "send")
        assert r.returncode != 0

    def test_duplicate_register(self, tmp_path: Path) -> None:
        run_cli(tmp_path, "register", "agent://a1", "--name", "A1", "--type", "agent")
        r = run_cli(tmp_path, "register", "agent://a1", "--name", "A1", "--type", "agent")
        assert r.returncode != 0
