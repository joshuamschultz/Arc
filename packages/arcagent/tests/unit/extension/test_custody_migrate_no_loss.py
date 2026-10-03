"""Hotfix: the connections.env migration never silently drops a credential.

Production (DGX, 9c280994): the startup migration moved 6 values, dropped 9 by
name (Jira/Confluence API tokens, Dropbox app key/secret, an access token) and
deleted the file. These tests pin the fixed contract:

* legacy app-credential keys a bundle declares (``[oauth] legacy_client_id_env``
  / ``legacy_client_secret_env``) go into the provider's sealed OAuth app slot;
* any other non-empty value blocks the automatic migration (the file is kept);
* only an explicit ``drop_undeclared`` drops a value, audited per key;
* the file is removed only when every value is moved (read back) or dropped.
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
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcagent.extension.grants import Connection
from arcagent.extension.oauth_apps import OAuthAppStore

REPO_EXTENSIONS = Path(__file__).resolve().parents[5] / "extensions"
CIPHER = ConnectorSecretCipher(b"\x0b" * 32)

#: The shape of the DGX file: one declared value, the Dropbox app pair in the
#: un-prefixed legacy spelling, and obsolete Atlassian API-token keys.
DGX_BODY = (
    "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-secret-1\n"
    "PERSONAL_DROPBOX_APP_KEY=dbxappkey123\n"
    "PERSONAL_DROPBOX_APP_SECRET=dbxappsecret456\n"
    "JIRA_API_TOKEN=jira-token-789\n"
    "CONFLUENCE_EMAIL=\n"
)


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
        world,
        audit=arcagent.AuditChain.held(sink),
        state_opener=opener,
        credential_cipher=CIPHER,
    )
    connections.registry.define(
        "work_slack", Connection(extension="slack", approval="auto", agents=())
    )
    connections.registry.define(
        "personal_dropbox", Connection(extension="dropbox", approval="auto", agents=())
    )
    return connections, backend, sink, config_file("connections.env", arc_dir)


async def test_undeclared_value_refuses_and_keeps_the_file(tmp_path: Path) -> None:
    connections, backend, sink, env = _deployment(tmp_path)
    _write_env(env, DGX_BODY)

    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.migrate_secrets()

    assert caught.value.code == "MIGRATION_UNDECLARED_KEYS"
    assert "JIRA_API_TOKEN" in caught.value.message
    assert "--drop-undeclared" in caught.value.message
    assert caught.value.details["keys"] == ["JIRA_API_TOKEN"]
    assert env.read_text() == DGX_BODY
    assert await backend.mutable_query(CREDENTIAL_COLLECTION) == []
    assert await OAuthAppStore(backend, CIPHER).get("dropbox") is None
    refused = [e for e in sink.events if e.action == "connection.credential.migration_refused"]
    assert len(refused) == 1 and refused[0].outcome == "deny"
    assert "jira-token-789" not in repr([e.model_dump() for e in sink.events])


async def test_dry_run_shows_moved_mapped_and_dropped(tmp_path: Path) -> None:
    connections, backend, _sink, env = _deployment(tmp_path)
    _write_env(env, DGX_BODY)

    report = await connections.migrate_secrets(dry_run=True)

    assert report.dry_run
    assert report.migrated == ("work_slack/user_token",)
    assert report.apps == ("dropbox",)
    assert report.app_keys == ("PERSONAL_DROPBOX_APP_KEY", "PERSONAL_DROPBOX_APP_SECRET")
    assert report.unresolved == (("JIRA_API_TOKEN", "undeclared"),)
    assert report.empty == ("CONFLUENCE_EMAIL",)
    assert env.read_text() == DGX_BODY
    assert await OAuthAppStore(backend, CIPHER).get("dropbox") is None


async def test_drop_undeclared_moves_maps_drops_audits_then_deletes(tmp_path: Path) -> None:
    connections, backend, sink, env = _deployment(tmp_path)
    _write_env(env, DGX_BODY)

    report = await connections.migrate_secrets(drop_undeclared=True)

    assert report.deleted and not env.exists()
    assert report.migrated == ("work_slack/user_token",)
    assert report.apps == ("dropbox",)
    assert report.dropped == ("JIRA_API_TOKEN",)
    app = await OAuthAppStore(backend, CIPHER).get("dropbox")
    assert app is not None
    assert app.client_id == "dbxappkey123"
    assert app.client_secret.reveal() == "dbxappsecret456"
    rows = CredentialRowStore(backend, CIPHER)
    row = await rows.read("work_slack")
    assert row is not None
    assert (await rows.open_field(row, "user_token")).reveal() == "xoxp-secret-1"  # type: ignore[union-attr]
    dropped = [e for e in sink.events if e.action == "connection.credential.dropped"]
    assert [e.extra["key"] for e in dropped] == ["JIRA_API_TOKEN"]
    assert any(e.action == "connection.credential.app_migrated" for e in sink.events)
    raw = repr([e.model_dump() for e in sink.events])
    assert "dbxappsecret456" not in raw and "jira-token-789" not in raw


async def test_only_mappable_values_migrate_without_a_flag(tmp_path: Path) -> None:
    """The prefixed legacy spelling maps too, and no flag is needed when nothing is lost."""
    connections, backend, _sink, env = _deployment(tmp_path)
    _write_env(
        env,
        "ARC_SECRET_PERSONAL_DROPBOX_APP_KEY=dbxappkey123\n"
        "ARC_SECRET_PERSONAL_DROPBOX_APP_SECRET=dbxappsecret456\n",
    )

    report = await connections.migrate_secrets()

    assert report.deleted and not env.exists()
    assert report.dropped == () and report.apps == ("dropbox",)
    assert (await OAuthAppStore(backend, CIPHER).get("dropbox")) is not None


async def test_value_that_differs_from_custody_is_never_overwritten(tmp_path: Path) -> None:
    """A stale file value (custody since rotated) neither overwrites nor vanishes silently."""
    connections, backend, _sink, env = _deployment(tmp_path)
    _write_env(env, "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-newer\n")
    await connections.migrate_secrets()
    _write_env(env, "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-older\n")

    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.migrate_secrets()

    assert caught.value.code == "MIGRATION_UNDECLARED_KEYS"
    assert env.exists()
    rows = CredentialRowStore(backend, CIPHER)
    row = await rows.read("work_slack")
    assert row is not None
    assert (await rows.open_field(row, "user_token")).reveal() == "xoxp-newer"  # type: ignore[union-attr]


async def test_app_slot_that_differs_is_never_overwritten(tmp_path: Path) -> None:
    connections, backend, _sink, env = _deployment(tmp_path)
    await OAuthAppStore(backend, CIPHER).put(
        "dropbox", client_id="setbyhand", client_secret="handsecret", actor_did="did:arc:x"
    )
    _write_env(
        env, "PERSONAL_DROPBOX_APP_KEY=dbxappkey123\nPERSONAL_DROPBOX_APP_SECRET=dbxappsecret456\n"
    )

    report = await connections.migrate_secrets(dry_run=True)

    assert report.apps == ()
    assert set(report.unresolved) == {
        ("PERSONAL_DROPBOX_APP_KEY", "app_slot_differs"),
        ("PERSONAL_DROPBOX_APP_SECRET", "app_slot_differs"),
    }
    app = await OAuthAppStore(backend, CIPHER).get("dropbox")
    assert app is not None and app.client_id == "setbyhand"


async def test_half_an_app_pair_is_not_mapped(tmp_path: Path) -> None:
    connections, _backend, _sink, env = _deployment(tmp_path)
    _write_env(env, "PERSONAL_DROPBOX_APP_KEY=dbxappkey123\n")

    report = await connections.migrate_secrets(dry_run=True)

    assert report.apps == ()
    assert report.unresolved == (("PERSONAL_DROPBOX_APP_KEY", "app_pair_incomplete"),)


def test_manifest_legacy_names_come_in_pairs() -> None:
    from arcagent.extension.manifest import OAuthFlow

    base: dict[str, Any] = {
        "provider": "dropbox",
        "authorize_url": "https://example.com/a",
        "token_url": "https://example.com/t",
        "refresh_token_secret": "refresh_token",
    }
    flow = OAuthFlow(**base, legacy_client_id_env="app_key", legacy_client_secret_env="app_secret")
    assert flow.legacy_client_id_env == "app_key"
    with pytest.raises(ValueError, match="legacy"):
        OAuthFlow(**base, legacy_client_id_env="app_key")
