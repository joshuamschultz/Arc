"""P18-2 §8.6 — arcui moves the legacy credential file into custody, or refuses to start."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import arcagent
import pytest
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcstore.backends.memory import FakeBackend
from arctrust.connector_cipher import ConnectorSecretCipher
from arctrust.paths import config_file

from arcui.credential_renewer import CredentialMigrationRefusedError, migrate_at_startup

REPO_EXTENSIONS = Path(__file__).resolve().parents[3] / "extensions"
CIPHER = ConnectorSecretCipher(b"\x07" * 32)


class _LossyReadBackFakeBackend(FakeBackend):
    """A custody plane that accepts writes but never reads a credential row back."""

    async def mutable_read(self, collection: str, key: str) -> dict[str, Any] | None:
        if collection == CREDENTIAL_COLLECTION:
            return None
        return await super().mutable_read(collection, key)


def _deployment(
    tmp_path: Path, backend: FakeBackend | None = None
) -> tuple[arcagent.Connections, FakeBackend, Path]:
    arc_dir = tmp_path / "arc"
    backend = backend or FakeBackend()

    async def opener() -> Any:
        return backend

    world = arcagent.resolve_deployment(arc_dir=arc_dir, extensions_root=REPO_EXTENSIONS)
    connections = arcagent.Connections(world, state_opener=opener, credential_cipher=CIPHER)
    connections.registry.define(
        "work_slack", arcagent.Connection(extension="slack", approval="auto", agents=())
    )
    connections.registry.define(
        "work_github", arcagent.Connection(extension="github", approval="auto", agents=())
    )
    return connections, backend, config_file("connections.env", arc_dir)


def _write_env(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, body.encode())
    os.close(fd)


async def test_startup_migrates_then_starts(tmp_path: Path) -> None:
    connections, backend, env = _deployment(tmp_path)
    _write_env(env, "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-secret-1\nEMPTY_LEFTOVER=\n")

    report = await migrate_at_startup(connections)

    assert not env.exists()
    assert report.migrated == ("work_slack/user_token",)
    assert report.dropped == () and report.empty == ("EMPTY_LEFTOVER",)
    rows = CredentialRowStore(backend, CIPHER)
    row = await rows.read("work_slack")
    assert row is not None
    assert (await rows.open_field(row, "user_token")).reveal() == "xoxp-secret-1"  # type: ignore[union-attr]
    raw = str(await backend.mutable_query(CREDENTIAL_COLLECTION))
    assert "xoxp-secret-1" not in raw
    # A second start has nothing to do.
    assert (await migrate_at_startup(connections)).skipped


async def test_startup_refuses_to_drop_an_undeclared_value(tmp_path: Path) -> None:
    """Hotfix (DGX 9c280994): startup never drops a credential it cannot place."""
    connections, backend, env = _deployment(tmp_path)
    body = "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-secret-1\nJIRA_API_TOKEN=orphan-2\n"
    _write_env(env, body)

    with pytest.raises(CredentialMigrationRefusedError) as caught:
        await migrate_at_startup(connections)

    message = str(caught.value)
    assert "JIRA_API_TOKEN" in message and "Custody panel" in message
    assert "orphan-2" not in message
    assert env.read_text() == body
    assert await backend.mutable_query(CREDENTIAL_COLLECTION) == []


async def test_startup_fails_closed_when_migration_cannot_complete(tmp_path: Path) -> None:
    connections, backend, env = _deployment(tmp_path)
    target = tmp_path / "elsewhere.env"
    _write_env(target, "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-secret-1\n")
    env.parent.mkdir(parents=True, exist_ok=True)
    env.symlink_to(target)

    with pytest.raises(CredentialMigrationRefusedError, match="Custody panel"):
        await migrate_at_startup(connections)

    assert env.is_symlink() and target.read_text().startswith("ARC_SECRET")
    assert await backend.mutable_query(CREDENTIAL_COLLECTION) == []


_TWO_VALUES = (
    "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-secret-1\nARC_SECRET_WORK_GITHUB_TOKEN=ghp-secret-2\n"
)


async def test_startup_moves_every_non_empty_value_and_proves_it_by_read_back(
    tmp_path: Path,
) -> None:
    connections, backend, env = _deployment(tmp_path)
    _write_env(env, _TWO_VALUES + "BLANK=\n")

    report = await migrate_at_startup(connections)

    assert sorted(report.migrated) == ["work_github/token", "work_slack/user_token"]
    rows = CredentialRowStore(backend, CIPHER)
    for connection, field, value in (
        ("work_slack", "user_token", "xoxp-secret-1"),
        ("work_github", "token", "ghp-secret-2"),
    ):
        row = await rows.read(connection)
        assert row is not None
        assert (await rows.open_field(row, field)).reveal() == value  # type: ignore[union-attr]
    assert not env.exists()


async def test_startup_refusal_leaves_the_file_byte_identical_with_several_undeclared(
    tmp_path: Path,
) -> None:
    """Regression for DGX 9c280994: no undeclared key may be dropped by a start."""
    connections, backend, env = _deployment(tmp_path)
    _write_env(env, _TWO_VALUES + "JIRA_API_TOKEN=orphan-2\nDROPBOX_ACCESS_TOKEN=orphan-3\n")
    before = env.read_bytes()

    with pytest.raises(CredentialMigrationRefusedError) as caught:
        await migrate_at_startup(connections)

    assert env.read_bytes() == before
    assert "JIRA_API_TOKEN" in str(caught.value) and "DROPBOX_ACCESS_TOKEN" in str(caught.value)
    assert "orphan" not in str(caught.value)
    assert await backend.mutable_query(CREDENTIAL_COLLECTION) == []


async def test_only_the_operator_choosing_drop_undeclared_lets_a_key_go(tmp_path: Path) -> None:
    connections, _backend, env = _deployment(tmp_path)
    _write_env(env, _TWO_VALUES + "JIRA_API_TOKEN=orphan-2\n")
    with pytest.raises(CredentialMigrationRefusedError):
        await migrate_at_startup(connections)
    assert env.exists()

    report = await connections.migrate_secrets(drop_undeclared=True)

    assert report.dropped == ("JIRA_API_TOKEN",)
    assert sorted(report.migrated) == ["work_github/token", "work_slack/user_token"]
    assert not env.exists()
    assert (await migrate_at_startup(connections)).skipped


async def test_startup_never_deletes_the_file_when_a_value_does_not_read_back(
    tmp_path: Path,
) -> None:
    connections, _backend, env = _deployment(tmp_path, _LossyReadBackFakeBackend())
    _write_env(env, _TWO_VALUES)
    before = env.read_bytes()

    with pytest.raises(CredentialMigrationRefusedError, match="MIGRATION_VERIFY_FAILED"):
        await migrate_at_startup(connections)

    assert env.read_bytes() == before
