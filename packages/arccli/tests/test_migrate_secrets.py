"""P18-2 §8.6 — ``arc connector migrate-secrets``: move, verify, delete, audit."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import arcagent
import pytest
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcagent.extension.custody_migrate import migrate_connector_secrets
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.secrets import SecretRef, SecretStore
from arcstore.backends.memory import FakeBackend
from arctrust.audit import AuditEvent
from arctrust.connector_cipher import ConnectorSecretCipher
from arctrust.paths import config_file

REPO_EXTENSIONS = Path(__file__).resolve().parents[3] / "extensions"
CIPHER = ConnectorSecretCipher(b"\x09" * 32)
ENV_BODY = (
    "ARC_SECRET_WORK_SLACK_USER_TOKEN=xoxp-secret-1\n"
    "JIRA_API_TOKEN=orphan-2\n"
    "PERSONAL_DROPBOX_ACCESS_TOKEN=orphan-3\n"
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


def _connections(tmp_path: Path, backend: FakeBackend, sink: ListSink) -> arcagent.Connections:
    async def opener() -> Any:
        return backend

    world = arcagent.resolve_deployment(arc_dir=tmp_path / "arc", extensions_root=REPO_EXTENSIONS)
    connections = arcagent.Connections(
        world,
        audit=arcagent.AuditChain.held(sink),
        state_opener=opener,
        credential_cipher=CIPHER,
    )
    connections.registry.define(
        "work_slack", Connection(extension="slack", approval="auto", agents=())
    )
    return connections


async def test_migrates_declared_drops_orphans_deletes_file_and_audits(tmp_path: Path) -> None:
    backend, sink = FakeBackend(), ListSink()
    connections = _connections(tmp_path, backend, sink)
    env = config_file("connections.env", tmp_path / "arc")
    _write_env(env, ENV_BODY)

    report = await connections.migrate_secrets()

    assert not env.exists()
    assert report.migrated == ("work_slack/user_token",)
    assert report.dropped == ("JIRA_API_TOKEN", "PERSONAL_DROPBOX_ACCESS_TOKEN")
    rows = CredentialRowStore(backend, CIPHER)
    row = await rows.read("work_slack")
    assert row is not None
    assert (await rows.open_field(row, "user_token")).reveal() == "xoxp-secret-1"  # type: ignore[union-attr]
    raw = str(await backend.mutable_query(CREDENTIAL_COLLECTION))
    assert "secret-1" not in raw and "orphan" not in raw
    migrated = [event for event in sink.events if event.action == "connection.credential.migrated"]
    assert len(migrated) == 1
    assert migrated[0].extra["dropped"] == ["JIRA_API_TOKEN", "PERSONAL_DROPBOX_ACCESS_TOKEN"]
    assert "secret-1" not in repr([event.model_dump() for event in sink.events])


async def test_read_back_mismatch_keeps_the_file(tmp_path: Path) -> None:
    backend = FakeBackend()
    env = tmp_path / "connections.env"
    _write_env(env, ENV_BODY)
    registry = ConnectionRegistry(tmp_path / "arc")
    registry.define("work_slack", Connection(extension="slack", approval="auto", agents=()))
    from arcagent.extension.custody import SealedCredentialBackend

    store = SecretStore(SealedCredentialBackend(CredentialRowStore(backend, CIPHER)))

    class Lying(SealedCredentialBackend):
        async def get(self, ref: SecretRef) -> str | None:
            return "not-what-was-written"

    liar = SecretStore(Lying(CredentialRowStore(backend, CIPHER)))
    with pytest.raises(arcagent.ExtensionError) as caught:
        await migrate_connector_secrets(
            env_path=env,
            registry=registry,
            declared_fields=lambda _instance, _extension: ("user_token",),
            secret_store=store,
            verify_store=lambda: liar,
            actor_did="did:arc:cli:test",
            sink=ListSink(),
        )
    assert caught.value.code == "MIGRATION_VERIFY_FAILED"
    assert env.read_text() == ENV_BODY


async def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    backend, sink = FakeBackend(), ListSink()
    connections = _connections(tmp_path, backend, sink)
    env = config_file("connections.env", tmp_path / "arc")
    _write_env(env, ENV_BODY)

    report = await connections.migrate_secrets(dry_run=True)

    assert report.dry_run and report.migrated == ("work_slack/user_token",)
    assert env.read_text() == ENV_BODY
    assert await backend.mutable_query(CREDENTIAL_COLLECTION) == []
    assert not [event for event in sink.events if event.action.startswith("connection.")]


def test_cli_verb_is_registered() -> None:
    from arccli.commands.connector import _build_parser

    args = _build_parser().parse_args(["migrate-secrets", "--dry-run"])
    assert args.subcmd == "migrate-secrets" and args.dry_run is True
    with pytest.raises(SystemExit):
        _build_parser().parse_args(["list", "--env-file", "x"])
