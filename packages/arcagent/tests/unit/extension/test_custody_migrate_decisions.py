"""J1-4: the operator answers each unplaceable credential key, one at a time.

A refused migration used to be all-or-nothing: ``--drop-undeclared`` dropped every
unplaceable value at once. The operator now decides per key from the dashboard:

* map the value onto a connection field Arc declares (stored and read back);
* keep it in the file (it stays, the migration no longer refuses over it);
* drop it on purpose (audited by name, never by value).

The never-drop guarantee holds: a key nobody decided still refuses, and the
read-back proof is the same one the automatic path uses.
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
from arcagent.extension.custody_migrate import KeyDecisions
from arcagent.extension.grants import Connection

REPO_EXTENSIONS = Path(__file__).resolve().parents[5] / "extensions"
CIPHER = ConnectorSecretCipher(b"\x0c" * 32)
BODY = "OLD_SLACK_TOKEN=xoxp-mapped-5\nJIRA_API_TOKEN=jira-token-789\nSTRAY_NOTE=keep-me-9\n"


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
    return connections, backend, sink, config_file("connections.env", arc_dir)


def _all(**overrides: Any) -> KeyDecisions:
    base: dict[str, Any] = {
        "mapped": {"OLD_SLACK_TOKEN": ("work_slack", "user_token")},
        "kept": frozenset({"STRAY_NOTE"}),
        "dropped": frozenset({"JIRA_API_TOKEN"}),
    }
    return KeyDecisions(**{**base, **overrides})


async def test_map_keep_and_drop_each_key_then_the_rest_migrates(tmp_path: Path) -> None:
    connections, backend, sink, env = _deployment(tmp_path)
    _write_env(env, BODY)

    report = await connections.migrate_secrets(decisions=_all())

    assert report.migrated == ("work_slack/user_token",)
    assert report.dropped == ("JIRA_API_TOKEN",)
    assert report.kept == ("STRAY_NOTE",)
    rows = CredentialRowStore(backend, CIPHER)
    row = await rows.read("work_slack")
    assert row is not None
    assert (await rows.open_field(row, "user_token")).reveal() == "xoxp-mapped-5"  # type: ignore[union-attr]
    assert env.read_text() == "STRAY_NOTE=keep-me-9\n"
    dropped = [e for e in sink.events if e.action == "connection.credential.dropped"]
    assert [e.extra["key"] for e in dropped] == ["JIRA_API_TOKEN"]
    kept = [e for e in sink.events if e.action == "connection.credential.kept"]
    assert [e.extra["key"] for e in kept] == ["STRAY_NOTE"]
    raw = repr([e.model_dump() for e in sink.events])
    for value in ("xoxp-mapped-5", "jira-token-789", "keep-me-9"):
        assert value not in raw


async def test_a_kept_key_no_longer_refuses_the_next_startup(tmp_path: Path) -> None:
    connections, _backend, _sink, env = _deployment(tmp_path)
    _write_env(env, "STRAY_NOTE=keep-me-9\n")
    await connections.migrate_secrets(decisions=KeyDecisions(kept=frozenset({"STRAY_NOTE"})))

    again = await connections.migrate_secrets()

    assert again.kept == ("STRAY_NOTE",)
    assert again.unresolved == ()
    assert env.read_text() == "STRAY_NOTE=keep-me-9\n"


async def test_an_undecided_key_still_refuses_and_nothing_is_written(tmp_path: Path) -> None:
    connections, backend, _sink, env = _deployment(tmp_path)
    _write_env(env, BODY)

    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.migrate_secrets(decisions=_all(dropped=frozenset()))

    assert caught.value.code == "MIGRATION_UNDECLARED_KEYS"
    assert caught.value.details["keys"] == ["JIRA_API_TOKEN"]
    assert env.read_text() == BODY
    assert await CredentialRowStore(backend, CIPHER).read("work_slack") is None


async def test_mapping_onto_a_field_no_connection_declares_is_refused(tmp_path: Path) -> None:
    connections, _backend, _sink, env = _deployment(tmp_path)
    _write_env(env, BODY)
    bad = _all(mapped={"OLD_SLACK_TOKEN": ("work_slack", "made_up_field")})

    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.migrate_secrets(decisions=bad)

    assert caught.value.code == "MIGRATION_BAD_DECISION"
    assert env.read_text() == BODY


async def test_a_decision_for_a_key_that_is_not_in_the_file_is_refused(tmp_path: Path) -> None:
    connections, _backend, _sink, env = _deployment(tmp_path)
    _write_env(env, BODY)

    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.migrate_secrets(
            decisions=_all(dropped=frozenset({"JIRA_API_TOKEN", "NOT_IN_FILE"}))
        )

    assert caught.value.code == "MIGRATION_BAD_DECISION"
    assert env.read_text() == BODY


async def test_one_key_cannot_get_two_answers(tmp_path: Path) -> None:
    connections, _backend, _sink, env = _deployment(tmp_path)
    _write_env(env, BODY)

    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.migrate_secrets(
            decisions=_all(dropped=frozenset({"JIRA_API_TOKEN", "STRAY_NOTE"}))
        )

    assert caught.value.code == "MIGRATION_BAD_DECISION"
    assert env.read_text() == BODY


async def test_mapping_over_a_different_stored_value_is_not_an_overwrite(tmp_path: Path) -> None:
    connections, backend, _sink, env = _deployment(tmp_path)
    rows = CredentialRowStore(backend, CIPHER)
    await rows.put_fields("work_slack", {"user_token": "xoxp-already-there"}, actor_did="did:t")
    _write_env(env, BODY)

    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.migrate_secrets(decisions=_all())

    assert caught.value.code == "MIGRATION_UNDECLARED_KEYS"
    row = await rows.read("work_slack")
    assert (await rows.open_field(row, "user_token")).reveal() == "xoxp-already-there"  # type: ignore[arg-type,union-attr]
    assert env.read_text() == BODY


async def test_the_dry_run_lists_keys_and_the_fields_a_key_may_map_to(tmp_path: Path) -> None:
    connections, _backend, _sink, env = _deployment(tmp_path)
    _write_env(env, BODY)

    report = await connections.migrate_secrets(dry_run=True)

    assert [key for key, _why in report.unresolved] == [
        "JIRA_API_TOKEN",
        "OLD_SLACK_TOKEN",
        "STRAY_NOTE",
    ]
    assert "work_slack/user_token" in report.targets
    assert "xoxp-mapped-5" not in repr(report)
