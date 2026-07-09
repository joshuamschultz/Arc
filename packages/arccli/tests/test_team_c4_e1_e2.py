"""Tests for SPEC-031 C4 (unified team CLI), E1 (messaging default), E2 (up/down).

All backend-touching tests inject a shared in-memory arcteam backend via the
``team_backend`` fixture, so they run without a NATS server. The send path is
exercised end-to-end (create a real agent → sign → store) to prove REQ-030:
outgoing messages are always signed.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from arccli.commands.team import (
    TeamSupervisor,
    _add_member,
    _build_supervisor,
    _channels,
    _create_team,
    _init_cmd,
    _read,
    _register,
    _remove_member,
    _send,
    _stop_pid,
    _thread,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_root(tmp_path: Path) -> Path:
    _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
    return tmp_path


def _query(backend: Any, collection: str) -> list[dict[str, Any]]:
    return asyncio.run(backend.query(collection))


def _stream(backend: Any, key: str) -> list[dict[str, Any]]:
    return asyncio.run(backend.read_stream("messages/streams", key, after_seq=0, limit=100))


def _register_user(root: Path, handle: str) -> None:
    _register(
        argparse.Namespace(
            root=str(root),
            entity_id=f"user://{handle}",
            name=handle.upper(),
            entity_type="user",
            roles="",
            workspace=None,
        )
    )


# ---------------------------------------------------------------------------
# E1 — team-member scaffold enables the messaging inbox loop by default
# ---------------------------------------------------------------------------


class TestMessagingEnabledByDefault:
    def test_default_config_enables_messaging_module(self) -> None:
        from arccli.commands.agent._common import _DEFAULT_CONFIG

        cfg = _DEFAULT_CONFIG.format(name="probe")
        assert "[modules.messaging]" in cfg
        # The messaging block must be enabled, not just present.
        block = cfg.split("[modules.messaging]", 1)[1]
        assert block.lstrip().startswith("enabled = true")


# ---------------------------------------------------------------------------
# C4 — team lifecycle verbs: create / add-member / remove-member
# ---------------------------------------------------------------------------


class TestTeamLifecycle:
    def test_create_team_persists(self, tmp_path: Path, team_backend: Any) -> None:
        root = _init_root(tmp_path)
        _create_team(
            argparse.Namespace(
                root=str(root),
                team_id="alpha",
                name="Alpha Squad",
                channel="general",
                members="",
                goal=None,
            )
        )
        teams = _query(team_backend, "teams")
        assert len(teams) == 1
        assert teams[0]["name"] == "Alpha Squad"
        assert teams[0]["default_channel"] == "general"

    def test_create_materializes_default_channel(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Creating a team with ``--channel ops`` materializes a real, listable
        channel that ``arc team channels`` enumerates (regression: the value was
        stored on the Team but never registered as a Channel)."""
        root = _init_root(tmp_path)
        _register_user(root, "bob")
        _create_team(
            argparse.Namespace(
                root=str(root),
                team_id="mfg",
                name="Mfg",
                channel="ops",
                members="user://bob",
                goal=None,
            )
        )

        # Enumerated by the same API `arc team channels` uses (list_channels).
        capsys.readouterr()
        _channels(argparse.Namespace(root=str(root), use_json=False))
        out = capsys.readouterr().out
        assert "ops" in out
        assert "No channels" not in out

        # The channel carries the resolved member DIDs so team send/read work.
        channels = _query(team_backend, "messages/channels")
        assert len(channels) == 1
        assert channels[0]["name"] == "ops"
        assert channels[0]["members"]
        assert channels[0]["members"][0].startswith("did:")

    def test_add_and_remove_member(self, tmp_path: Path, team_backend: Any) -> None:
        root = _init_root(tmp_path)
        _register_user(root, "bob")
        _create_team(
            argparse.Namespace(
                root=str(root),
                team_id="alpha",
                name="Alpha",
                channel="general",
                members="",
                goal=None,
            )
        )

        _add_member(argparse.Namespace(root=str(root), team_id="alpha", member="user://bob"))
        members = _query(team_backend, "teams")[0]["members"]
        assert len(members) == 1
        assert members[0].startswith("did:")

        _remove_member(argparse.Namespace(root=str(root), team_id="alpha", member="user://bob"))
        assert _query(team_backend, "teams")[0]["members"] == []


# ---------------------------------------------------------------------------
# C4 + REQ-030 — send always signs; inbox/read/thread read back
# ---------------------------------------------------------------------------


def _create_agent(tmp_path: Path, name: str) -> str:
    """Scaffold a real agent (mints identity, registers) and return its DID."""
    from arccli.commands.agent.create import _create

    _create(
        argparse.Namespace(
            name=name,
            parent_dir=str(tmp_path),
            model="anthropic/claude-sonnet-4-5-20250929",
            no_register=False,
        )
    )
    import tomllib

    cfg = tomllib.loads((tmp_path / name / "arcagent.toml").read_text())
    return str(cfg["identity"]["did"])


class TestSendSigned:
    def test_send_signs_outgoing_message(
        self, tmp_path: Path, team_backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        sender_did = _create_agent(tmp_path, "sender")

        _send(
            argparse.Namespace(
                root=None,
                sender="agent://sender",
                to="agent://receiver",
                body="hello team",
                type=None,
                priority=None,
                action=False,
                refs=None,
                thread_id=None,
            )
        )

        stored = _stream(team_backend, "arc.agent.receiver")
        assert len(stored) == 1
        assert stored[0]["body"] == "hello team"
        # REQ-030: the message is signed by the sender's arctrust identity.
        assert stored[0]["sig"] != ""
        assert stored[0]["signer_did"] == sender_did

    def test_read_and_thread_return_message(
        self, tmp_path: Path, team_backend: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        _create_agent(tmp_path, "sender")
        _send(
            argparse.Namespace(
                root=None,
                sender="agent://sender",
                to="agent://receiver",
                body="ping",
                type=None,
                priority=None,
                action=False,
                refs=None,
                thread_id=None,
            )
        )
        stored = _stream(team_backend, "arc.agent.receiver")
        thread_id = stored[0]["thread_id"]

        # read --dm receiver
        _read(
            argparse.Namespace(
                root=None,
                sender="agent://receiver",
                channel=None,
                dm="receiver",
                limit=20,
                use_json=False,
            )
        )
        # thread by id
        _thread(
            argparse.Namespace(
                root=None,
                thread_id=thread_id,
                stream="arc.agent.receiver",
                use_json=False,
            )
        )


# ---------------------------------------------------------------------------
# E2 — supervised daemon orchestrator
# ---------------------------------------------------------------------------


class _BlockProc:
    def __init__(self) -> None:
        self._done = asyncio.Event()

    async def wait(self) -> int:
        await self._done.wait()
        return 0

    def terminate(self) -> None:
        self._done.set()


class _CrashProc:
    async def wait(self) -> int:
        return 1

    def terminate(self) -> None:
        pass


class TestTeamSupervisor:
    def test_spawns_each_member_and_stops(self) -> None:
        spawned: list[str] = []

        async def spawn(name: str, agent_dir: str) -> Any:
            spawned.append(name)
            return _BlockProc()

        sup = TeamSupervisor({"a": "/tmp/a", "b": "/tmp/b"}, spawn=spawn)

        async def _drive() -> None:
            task = asyncio.create_task(sup.run())
            await asyncio.sleep(0.05)
            assert set(spawned) == {"a", "b"}
            sup.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(_drive())

    def test_restarts_crashed_member(self) -> None:
        procs: list[Any] = [_CrashProc(), _BlockProc()]
        spawn_count = 0

        async def spawn(name: str, agent_dir: str) -> Any:
            nonlocal spawn_count
            spawn_count += 1
            return procs.pop(0)

        sup = TeamSupervisor({"a": "/tmp/a"}, spawn=spawn, backoff=0.01)

        async def _drive() -> None:
            task = asyncio.create_task(sup.run())
            await asyncio.sleep(0.1)
            assert spawn_count == 2  # crashed once → restarted
            sup.stop()
            await asyncio.wait_for(task, timeout=2)

        asyncio.run(_drive())

    def test_build_supervisor_maps_members_to_agent_dirs(
        self, tmp_path: Path, team_backend: Any
    ) -> None:
        root = _init_root(tmp_path)
        agent_dir = tmp_path / "worker"
        ws = agent_dir / "workspace"
        ws.mkdir(parents=True)
        _register(
            argparse.Namespace(
                root=str(root),
                entity_id="agent://worker",
                name="Worker",
                entity_type="agent",
                roles="",
                workspace=str(ws),
            )
        )
        _create_team(
            argparse.Namespace(
                root=str(root),
                team_id="alpha",
                name="Alpha",
                channel="general",
                members="agent://worker",
                goal=None,
            )
        )

        async def spawn(name: str, agent_dir: str) -> Any:
            return _BlockProc()

        sup = asyncio.run(_build_supervisor(root, "alpha", spawn=spawn))
        assert sup.targets == {"worker": str(agent_dir)}


class TestTeamDown:
    def test_stop_pid_sends_signal(self, tmp_path: Path) -> None:
        pid_file = tmp_path / "team.pid"
        pid_file.write_text("4242\n")
        killed: list[int] = []

        _stop_pid(pid_file, kill=lambda pid, sig: killed.append(pid))
        assert killed == [4242]
        assert not pid_file.exists()
