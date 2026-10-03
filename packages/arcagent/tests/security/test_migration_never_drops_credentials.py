"""Battery: the legacy credential-file migration never silently drops a credential.

DGX 9c280994 lost nine values when the startup migration dropped every key no
bundle declared and deleted the file. The abuse cases here:

* a startup migration meets an undeclared value: nothing is written or deleted;
* a planted (or stale) legacy file cannot overwrite what custody already holds,
  even with ``drop_undeclared`` (the newer custody value wins; the file value is
  dropped on purpose, audited by name);
* a planted legacy app pair cannot replace a configured OAuth app slot (sign-in
  app hijack), even with ``drop_undeclared``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from arctrust.connector_cipher import ConnectorSecretCipher
from arctrust.paths import config_file

import arcagent
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.grants import Connection
from arcagent.extension.oauth_apps import OAuthAppStore

REPO_EXTENSIONS = Path(__file__).resolve().parents[4] / "extensions"
CIPHER = ConnectorSecretCipher(b"\x0c" * 32)


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _write_env(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.write(fd, body.encode())
    os.close(fd)


def _deployment(tmp_path: Path) -> tuple[arcagent.Connections, FakeBackend, ListSink, Path]:
    backend, sink = FakeBackend(), ListSink()

    async def opener() -> Any:
        return backend

    arc_dir = tmp_path / "arc"
    world = arcagent.resolve_deployment(arc_dir=arc_dir, extensions_root=REPO_EXTENSIONS)
    connections = arcagent.Connections(
        world, audit=arcagent.AuditChain.held(sink), state_opener=opener, credential_cipher=CIPHER
    )
    connections.registry.define(
        "work_slack", Connection(extension="slack", approval="auto", agents=())
    )
    connections.registry.define(
        "personal_dropbox", Connection(extension="dropbox", approval="auto", agents=())
    )
    return connections, backend, sink, config_file("connections.env", arc_dir)


async def test_startup_style_migration_never_drops_an_undeclared_value(tmp_path: Path) -> None:
    connections, backend, _sink, env = _deployment(tmp_path)
    body = "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-1\nCONFLUENCE_API_TOKEN=atl-token\n"
    _write_env(env, body)

    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.migrate_secrets()

    assert caught.value.code == "MIGRATION_UNDECLARED_KEYS"
    assert env.read_text() == body
    assert await CredentialRowStore(backend, CIPHER).read("work_slack") is None


async def test_stale_file_cannot_overwrite_rotated_custody(tmp_path: Path) -> None:
    connections, backend, sink, env = _deployment(tmp_path)
    _write_env(env, "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-current\n")
    await connections.migrate_secrets()
    _write_env(env, "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-stale\n")

    report = await connections.migrate_secrets(drop_undeclared=True)

    assert report.dropped == ("ARC_SECRET_WORK_SLACK_USER_TOKEN",) and report.migrated == ()
    rows = CredentialRowStore(backend, CIPHER)
    row = await rows.read("work_slack")
    assert row is not None
    assert (await rows.open_field(row, "user_token")).reveal() == "xoxp-current"  # type: ignore[union-attr]
    dropped = [e for e in sink.events if e.action == "connection.credential.dropped"]
    assert [(e.extra["key"], e.extra["reason"]) for e in dropped] == [
        ("ARC_SECRET_WORK_SLACK_USER_TOKEN", "custody_differs")
    ]


async def test_planted_app_pair_cannot_hijack_a_configured_slot(tmp_path: Path) -> None:
    connections, backend, _sink, env = _deployment(tmp_path)
    apps = OAuthAppStore(backend, CIPHER)
    await apps.put("dropbox", client_id="realapp", client_secret="realsecret", actor_did="did:x")
    _write_env(
        env, "PERSONAL_DROPBOX_APP_KEY=attackerapp\nPERSONAL_DROPBOX_APP_SECRET=attackersecret\n"
    )

    with pytest.raises(arcagent.ExtensionError):
        await connections.migrate_secrets()
    report = await connections.migrate_secrets(drop_undeclared=True)

    assert report.apps == ()
    app = await apps.get("dropbox")
    assert app is not None
    assert (app.client_id, app.client_secret.reveal()) == ("realapp", "realsecret")
