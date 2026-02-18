"""Unit tests for arcteam.cli — direct main() calls for coverage."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from arcteam.cli import main


def _r(path: Path) -> list[str]:
    """Build the --root prefix for CLI args."""
    return ["--root", str(path)]


class TestRegisterAndEntities:
    """Register entities and list them."""

    def test_register_agent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        out = capsys.readouterr().out
        assert "Registered agent: agent://a1" in out

    def test_register_user(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "user://josh",
              "--name", "Josh", "--type", "user", "--roles", "admin,ops"])
        out = capsys.readouterr().out
        assert "Registered user: user://josh" in out

    def test_entities_human(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent", "--roles", "ops"])
        capsys.readouterr()
        main([*_r(tmp_path), "entities"])
        out = capsys.readouterr().out
        assert "Entities (1):" in out
        assert "agent://a1" in out

    def test_entities_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        capsys.readouterr()
        main([*_r(tmp_path), "--json", "entities"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert len(data) == 1
        assert data[0]["id"] == "agent://a1"

    def test_entities_filter_by_role(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent", "--roles", "ops"])
        main([*_r(tmp_path), "register", "agent://a2",
              "--name", "A2", "--type", "agent", "--roles", "dev"])
        capsys.readouterr()
        main([*_r(tmp_path), "entities", "--role", "ops"])
        out = capsys.readouterr().out
        assert "agent://a1" in out
        assert "agent://a2" not in out


class TestChannels:
    """Channel create, join, and list."""

    def test_channel_create_and_list(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "channel", "ops",
              "--members", "agent://a1", "--description", "Operations"])
        out = capsys.readouterr().out
        assert "Channel created: ops" in out

        main([*_r(tmp_path), "channels"])
        out = capsys.readouterr().out
        assert "ops" in out

    def test_channels_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "channel", "ops", "--members", "agent://a1"])
        capsys.readouterr()
        main([*_r(tmp_path), "--json", "channels"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)
        assert len(data) == 1

    def test_join_channel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "channel", "ops", "--members", "agent://a1"])
        capsys.readouterr()
        main([*_r(tmp_path), "join", "ops", "agent://a2"])
        out = capsys.readouterr().out
        assert "Joined channel: ops" in out


class TestSendAndInbox:
    """Send messages and check inbox."""

    def _setup_agents(self, root: Path) -> None:
        main([*_r(root), "register", "agent://a1",
              "--name", "A1", "--type", "agent", "--roles", "ops"])
        main([*_r(root), "register", "agent://a2",
              "--name", "A2", "--type", "agent", "--roles", "ops"])
        main([*_r(root), "channel", "ops",
              "--members", "agent://a1,agent://a2"])

    def test_send_and_inbox(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._setup_agents(tmp_path)
        capsys.readouterr()

        main([*_r(tmp_path), "--as", "agent://a1", "send",
              "--to", "channel://ops", "--body", "Hello!"])
        out = capsys.readouterr().out
        assert "Sent:" in out

        main([*_r(tmp_path), "--as", "agent://a2", "inbox"])
        out = capsys.readouterr().out
        assert "Hello!" in out

    def test_inbox_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        self._setup_agents(tmp_path)
        main([*_r(tmp_path), "--as", "agent://a1", "send",
              "--to", "channel://ops", "--body", "Hi"])
        capsys.readouterr()

        main([*_r(tmp_path), "--as", "agent://a2", "--json", "inbox"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, dict)

    def test_inbox_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        capsys.readouterr()
        main([*_r(tmp_path), "--as", "agent://a1", "inbox"])
        out = capsys.readouterr().out
        assert "No new messages" in out

    def test_send_dm(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        main([*_r(tmp_path), "register", "user://josh",
              "--name", "Josh", "--type", "user"])
        capsys.readouterr()

        main([*_r(tmp_path), "--as", "user://josh", "send",
              "--to", "agent://a1", "--body", "Hey agent!",
              "--type", "request", "--priority", "high", "--action"])
        out = capsys.readouterr().out
        assert "Sent:" in out

    def test_send_with_refs_and_reply(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        capsys.readouterr()

        main([*_r(tmp_path), "--as", "agent://a1", "send",
              "--to", "agent://a1", "--body", "Self msg",
              "--refs", "ref1,ref2", "--thread-id", "msg_123"])
        out = capsys.readouterr().out
        assert "Sent:" in out


class TestReadAndThread:
    """Read channel/DM history and thread view."""

    def test_read_channel(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        main([*_r(tmp_path), "channel", "ops",
              "--members", "agent://a1"])
        main([*_r(tmp_path), "--as", "agent://a1", "send",
              "--to", "channel://ops", "--body", "Channel msg"])
        capsys.readouterr()

        main([*_r(tmp_path), "--as", "agent://a1", "read",
              "--channel", "ops"])
        out = capsys.readouterr().out
        assert "Channel msg" in out

    def test_read_channel_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        main([*_r(tmp_path), "channel", "ops",
              "--members", "agent://a1"])
        main([*_r(tmp_path), "--as", "agent://a1", "send",
              "--to", "channel://ops", "--body", "JSON msg"])
        capsys.readouterr()

        main([*_r(tmp_path), "--as", "agent://a1", "--json", "read",
              "--channel", "ops"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)

    def test_read_dm(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        main([*_r(tmp_path), "register", "user://josh",
              "--name", "Josh", "--type", "user"])
        main([*_r(tmp_path), "--as", "user://josh", "send",
              "--to", "agent://a1", "--body", "DM msg"])
        capsys.readouterr()

        main([*_r(tmp_path), "--as", "agent://a1", "read",
              "--dm", "a1"])
        out = capsys.readouterr().out
        assert "DM msg" in out

    def test_read_no_channel_or_dm_fails(self, tmp_path: Path) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        with pytest.raises(SystemExit):
            main([*_r(tmp_path), "--as", "agent://a1", "read"])

    def test_thread_view(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        main([*_r(tmp_path), "channel", "ops",
              "--members", "agent://a1"])
        capsys.readouterr()

        main([*_r(tmp_path), "--as", "agent://a1", "send",
              "--to", "channel://ops", "--body", "Thread root"])
        out = capsys.readouterr().out
        # Extract message ID from "Sent: msg_XXX (seq=N)"
        msg_id = out.strip().split("Sent: ")[1].split(" ")[0]

        main([*_r(tmp_path), "--as", "agent://a1", "thread", msg_id,
              "--stream", "arc.channel.ops"])
        out = capsys.readouterr().out
        assert "Thread root" in out

    def test_thread_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        main([*_r(tmp_path), "channel", "ops",
              "--members", "agent://a1"])
        main([*_r(tmp_path), "--as", "agent://a1", "send",
              "--to", "channel://ops", "--body", "Thread root"])
        out = capsys.readouterr().out
        msg_id = out.strip().split("Sent: ")[1].split(" ")[0]

        main([*_r(tmp_path), "--as", "agent://a1", "--json",
              "thread", msg_id, "--stream", "arc.channel.ops"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)


class TestDLQAndAudit:
    """DLQ and audit log commands."""

    def test_dlq_empty(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "dlq"])
        out = capsys.readouterr().out
        assert "DLQ (0 entries)" in out

    def test_dlq_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "--json", "dlq"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)

    def test_audit_log(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        capsys.readouterr()
        main([*_r(tmp_path), "audit"])
        out = capsys.readouterr().out
        assert "Audit log" in out
        assert "entity.registered" in out

    def test_audit_json(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str],
    ) -> None:
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        capsys.readouterr()
        main([*_r(tmp_path), "--json", "audit"])
        out = capsys.readouterr().out
        data = json.loads(out)
        assert isinstance(data, list)

    def test_audit_verify(
        self,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        # Persistent key required for cross-invocation chain verification
        monkeypatch.setenv("ARCTEAM_HMAC_KEY", "test-verify-key")
        main([*_r(tmp_path), "register", "agent://a1",
              "--name", "A1", "--type", "agent"])
        capsys.readouterr()
        main([*_r(tmp_path), "audit", "--verify"])
        out = capsys.readouterr().out
        assert "VALID" in out
