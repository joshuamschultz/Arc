"""Tests for `arc team create-channel` and `arc team update-entity`.

Both wire ALREADY-EXISTING arcteam service methods that had no CLI exposure:
- MessagingService.create_channel — previously only reachable via the
  private `_create_team` default-channel materialization step.
- EntityRegistry.update — previously reachable from no CLI command at all;
  `arc team register` is a strict create (raises on duplicate DID/handle),
  so there was no way to fix a mis-set name/role on an already-registered
  entity without a hand-written service-API script.

Follows the same in-memory `team_backend` fixture pattern as
test_team_c4_e1_e2.py so these run without a live NATS server.
"""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path
from typing import Any

import pytest

from arccli.commands.team import (
    _channels,
    _create_channel,
    _create_team,
    _init_cmd,
    _register,
    _update_entity,
)

# ---------------------------------------------------------------------------
# Helpers (mirrors test_team_c4_e1_e2.py)
# ---------------------------------------------------------------------------


def _init_root(tmp_path: Path) -> Path:
    _init_cmd(argparse.Namespace(root_path=str(tmp_path)))
    return tmp_path


def _query(backend: Any, collection: str) -> list[dict[str, Any]]:
    return asyncio.run(backend.query(collection))


def _register_user(root: Path, handle: str, *, name: str | None = None, roles: str = "") -> None:
    _register(
        argparse.Namespace(
            root=str(root),
            entity_id=f"user://{handle}",
            name=name or handle.upper(),
            entity_type="user",
            roles=roles,
            workspace=None,
        )
    )


def _make_team(root: Path, team_id: str, *, channel: str, members: str = "") -> None:
    _create_team(
        argparse.Namespace(
            root=str(root),
            team_id=team_id,
            name=team_id,
            channel=channel,
            members=members,
            goal=None,
        )
    )


# ---------------------------------------------------------------------------
# arc team create-channel
# ---------------------------------------------------------------------------


class TestCreateChannel:
    def test_standalone_channel_with_explicit_members(
        self, tmp_path: Path, team_backend: Any
    ) -> None:
        root = _init_root(tmp_path)
        _register_user(root, "bob")

        _create_channel(
            argparse.Namespace(
                root=str(root),
                channel_name="ops-room",
                members="user://bob",
                team=None,
            )
        )

        channels = _query(team_backend, "messages/channels")
        assert len(channels) == 1
        assert channels[0]["name"] == "ops-room"
        assert channels[0]["members"]
        assert channels[0]["members"][0].startswith("did:")

    def test_channel_with_no_members_creates_empty_channel(
        self, tmp_path: Path, team_backend: Any
    ) -> None:
        root = _init_root(tmp_path)

        _create_channel(
            argparse.Namespace(root=str(root), channel_name="empty-room", members="", team=None)
        )

        channels = _query(team_backend, "messages/channels")
        assert len(channels) == 1
        assert channels[0]["members"] == []

    def test_team_scoped_channel_defaults_members_to_team_members(
        self, tmp_path: Path, team_backend: Any
    ) -> None:
        """--team without --members defaults membership to the team's own members."""
        root = _init_root(tmp_path)
        _register_user(root, "bob")
        _make_team(root, "mfg", channel="general", members="user://bob")

        _create_channel(
            argparse.Namespace(root=str(root), channel_name="mfg-alerts", members="", team="mfg")
        )

        channels = {c["name"]: c for c in _query(team_backend, "messages/channels")}
        assert "mfg-alerts" in channels
        assert channels["mfg-alerts"]["members"] == channels["general"]["members"]

    def test_team_scoped_channel_explicit_members_override_team_members(
        self, tmp_path: Path, team_backend: Any
    ) -> None:
        root = _init_root(tmp_path)
        _register_user(root, "bob")
        _register_user(root, "carol")
        _make_team(root, "mfg", channel="general", members="user://bob")

        _create_channel(
            argparse.Namespace(
                root=str(root), channel_name="mfg-vip", members="user://carol", team="mfg"
            )
        )

        channels = {c["name"]: c for c in _query(team_backend, "messages/channels")}
        # The new channel's members must be carol's DID, not bob's (team default).
        assert channels["mfg-vip"]["members"] != channels["general"]["members"]

    def test_unknown_team_id_exits_with_error(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _init_root(tmp_path)

        with pytest.raises(SystemExit):
            _create_channel(
                argparse.Namespace(
                    root=str(root), channel_name="ghost-room", members="", team="no-such-team"
                )
            )

        err = capsys.readouterr().err
        assert "no-such-team" in err

    def test_duplicate_channel_name_refused(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """create_channel() itself has no duplicate guard — the CLI must add one
        so a second `create-channel` call doesn't silently wipe the first
        channel's membership."""
        root = _init_root(tmp_path)
        _register_user(root, "bob")
        _create_channel(
            argparse.Namespace(
                root=str(root), channel_name="ops-room", members="user://bob", team=None
            )
        )

        with pytest.raises(SystemExit):
            _create_channel(
                argparse.Namespace(root=str(root), channel_name="ops-room", members="", team=None)
            )

        err = capsys.readouterr().err
        assert "ops-room" in err
        # The original membership must survive the refused second call.
        channels = _query(team_backend, "messages/channels")
        assert len(channels) == 1
        assert channels[0]["members"]

    def test_channel_listed_by_arc_team_channels(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _init_root(tmp_path)
        _create_channel(
            argparse.Namespace(root=str(root), channel_name="brand-room", members="", team=None)
        )

        capsys.readouterr()
        _channels(argparse.Namespace(root=str(root), use_json=False))
        out = capsys.readouterr().out
        assert "brand-room" in out


# ---------------------------------------------------------------------------
# arc team update-entity
# ---------------------------------------------------------------------------


class TestUpdateEntity:
    def test_updates_name_and_roles(self, tmp_path: Path, team_backend: Any) -> None:
        root = _init_root(tmp_path)
        _register_user(root, "bob", name="Bob", roles="executor")

        _update_entity(
            argparse.Namespace(
                root=str(root), entity_ref="user://bob", name="Bob Marketing", roles="marketer"
            )
        )

        entities = _query(team_backend, "messages/registry")
        assert len(entities) == 1
        assert entities[0]["name"] == "Bob Marketing"
        assert entities[0]["roles"] == ["marketer"]

    def test_partial_update_leaves_unset_field_unchanged(
        self, tmp_path: Path, team_backend: Any
    ) -> None:
        """Omitting --roles must not clobber the entity's existing roles."""
        root = _init_root(tmp_path)
        _register_user(root, "bob", name="Bob", roles="executor,writer")

        _update_entity(
            argparse.Namespace(
                root=str(root), entity_ref="user://bob", name="Bob Renamed", roles=None
            )
        )

        entities = _query(team_backend, "messages/registry")
        assert entities[0]["name"] == "Bob Renamed"
        assert entities[0]["roles"] == ["executor", "writer"]

    def test_unknown_entity_exits_with_error(
        self, tmp_path: Path, team_backend: Any, capsys: pytest.CaptureFixture[str]
    ) -> None:
        root = _init_root(tmp_path)

        with pytest.raises(SystemExit):
            _update_entity(
                argparse.Namespace(root=str(root), entity_ref="user://ghost", name="X", roles=None)
            )

        err = capsys.readouterr().err
        assert "ghost" in err

    def test_update_does_not_change_did_or_handle(self, tmp_path: Path, team_backend: Any) -> None:
        """update-entity edits display fields only — identity stays stable."""
        root = _init_root(tmp_path)
        _register_user(root, "bob", name="Bob", roles="executor")
        before = _query(team_backend, "messages/registry")[0]

        _update_entity(
            argparse.Namespace(
                root=str(root), entity_ref="user://bob", name="New Name", roles=None
            )
        )

        after = _query(team_backend, "messages/registry")[0]
        assert after["did"] == before["did"]
        assert after["handle"] == before["handle"]
