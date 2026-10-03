"""P18-2F — ``arc connector migrate-secrets --reseal``: in-process rows into the vault."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import arcagent
import pytest
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcagent.extension.custody_migrate import ResealReport
from arcstore.backends.memory import FakeBackend
from arctrust import FileNotaryTransit, OperatorKey
from arctrust.audit import AuditEvent
from arctrust.connector_cipher import ConnectorSecretCipher
from arctrust.operator_resolver import MachineSecurity, operator_key_file
from arctrust.paths import config_file

from arccli.commands import connector

REPO_EXTENSIONS = Path(__file__).resolve().parents[3] / "extensions"
ACTOR = "did:arc:cli:test"


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


def _deployment(tmp_path: Path, custody: str = "vault_transit") -> tuple[Path, Any]:
    """An enterprise deployment that ran in_process, now switched to ``custody``."""
    arc_dir = tmp_path / "arc"
    keystore = tmp_path / "notary"
    old_key = OperatorKey.load(
        operator_key_file(MachineSecurity(), base=arc_dir), generate_if_absent=True
    )
    FileNotaryTransit.provision(keystore, "operator", old_key.seed)
    toml = config_file("arcagent.toml", arc_dir)
    toml.parent.mkdir(parents=True, exist_ok=True)
    toml.write_text(
        f'[security]\ntier = "enterprise"\ncustody = "{custody}"\n'
        f'notary_keystore = "{keystore}"\n',
        encoding="utf-8",
    )
    return arc_dir, ConnectorSecretCipher.for_operator_key(old_key)


def _connections(arc_dir: Path, backend: FakeBackend, sink: ListSink) -> Any:
    async def opener() -> Any:
        return backend

    world = arcagent.resolve_deployment(arc_dir=arc_dir, extensions_root=REPO_EXTENSIONS)
    return arcagent.Connections(world, audit=arcagent.AuditChain.held(sink), state_opener=opener)


async def test_reseal_moves_in_process_rows_into_the_vault_and_is_idempotent(
    tmp_path: Path,
) -> None:
    arc_dir, old = _deployment(tmp_path)
    backend, sink = FakeBackend(), ListSink()
    await CredentialRowStore(backend, old).put_fields(
        "work_slack", {"user_token": "xoxp-secret-9"}, actor_did=ACTOR
    )
    connections = _connections(arc_dir, backend, sink)

    planned = await connections.reseal_secrets(dry_run=True)
    assert planned.pending == ("work_slack",)
    assert (await backend.mutable_query(CREDENTIAL_COLLECTION))[0]["cipher"] == "xc1"

    report = await connections.reseal_secrets()
    assert report.resealed == ("work_slack",)
    rows = await backend.mutable_query(CREDENTIAL_COLLECTION)
    assert rows[0]["cipher"] == "transit1"
    assert "xoxp-secret-9" not in str(rows)
    custody = await connections._custody(sink)
    row = await custody.rows.read("work_slack")
    assert row is not None
    found = await custody.rows.open_field(row, "user_token")
    assert found is not None and found.reveal() == "xoxp-secret-9"
    assert any(event.action == "connection.credential.resealed" for event in sink.events)
    assert "xoxp-secret-9" not in repr([event.model_dump() for event in sink.events])

    again = await connections.reseal_secrets()
    assert again.resealed == ()
    assert again.already == ("work_slack",)


async def test_reseal_refuses_on_an_in_process_deployment(tmp_path: Path) -> None:
    arc_dir, _ = _deployment(tmp_path, custody="in_process")
    connections = _connections(arc_dir, FakeBackend(), ListSink())
    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.reseal_secrets()
    assert caught.value.code == "RESEAL_NOT_APPLICABLE"


async def test_reseal_without_the_old_key_says_connect_again(tmp_path: Path) -> None:
    arc_dir, _ = _deployment(tmp_path)
    operator_key_file(MachineSecurity(), base=arc_dir).unlink()
    connections = _connections(arc_dir, FakeBackend(), ListSink())
    with pytest.raises(arcagent.ExtensionError) as caught:
        await connections.reseal_secrets()
    assert caught.value.code == "RESEAL_SOURCE_KEY_MISSING"
    assert "connected again" in caught.value.message


def test_cli_reseal_prints_names_only(capsys: pytest.CaptureFixture[str]) -> None:
    class Fake:
        async def reseal_secrets(self, *, dry_run: bool) -> ResealReport:
            return ResealReport(resealed=("work_slack",), already=("blackarc",))

    connector._reseal_secrets(Fake(), dry_run=False)
    out = capsys.readouterr().out
    assert "Re-sealed 1 connection(s)" in out
    assert "resealed: work_slack" in out
    assert "already : blackarc" in out
