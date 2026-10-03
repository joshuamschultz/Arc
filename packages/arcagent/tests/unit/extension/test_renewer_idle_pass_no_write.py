"""Hotfix: an idle credential-renewer pass writes nothing to the connection rows.

DGX 9c280994: ``blackarc``, ``jira`` and ``systems`` reached ~30 ``revision``s
within half an hour of start: every 60 s pass re-reported the same terminal
failure (``credential_missing`` on a custody row with no grant), and every report
was a compare-and-set that bumped ``revision`` and ``last_checked_at``. A repeat
of a state already recorded is not news and is not written.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.connector_cipher import ConnectorSecretCipher

import arcagent
from arcagent.extension.custody import CredentialRowStore
from arcagent.extension.grants import Connection

REPO_EXTENSIONS = Path(__file__).resolve().parents[5] / "extensions"
CIPHER = ConnectorSecretCipher(b"\x0e" * 32)


def _connections(tmp_path: Path) -> tuple[arcagent.Connections, FakeBackend]:
    backend = FakeBackend()

    async def opener() -> Any:
        return backend

    world = arcagent.resolve_deployment(arc_dir=tmp_path / "arc", extensions_root=REPO_EXTENSIONS)
    connections = arcagent.Connections(world, state_opener=opener, credential_cipher=CIPHER)
    connections.registry.define("jira", Connection(extension="jira", approval="auto"))
    return connections, backend


async def _revision(connections: arcagent.Connections) -> int:
    return (await connections.health_records())["jira"].revision


async def test_repeated_terminal_failure_is_written_once(tmp_path: Path) -> None:
    """A row with a site but no grant: the first pass records it, later passes do not."""
    connections, backend = _connections(tmp_path)
    await connections.ensure_health_records()
    await CredentialRowStore(backend, CIPHER).put_fields(
        "jira", {"site": "example.atlassian.net"}, actor_did="did:arc:test"
    )

    first = await connections.renew_credentials()
    settled = await _revision(connections)
    for _ in range(5):
        assert await connections.renew_credentials() == first

    record = (await connections.health_records())["jira"]
    assert (record.status, record.reason_code) == ("needs_you", "credential_missing")
    assert record.revision == settled


@pytest.mark.parametrize("passes", [3])
async def test_fresh_token_pass_does_not_write(tmp_path: Path, passes: int) -> None:
    connections, backend = _connections(tmp_path)
    await connections.ensure_health_records()
    now = datetime.now(UTC)
    await CredentialRowStore(backend, CIPHER).put_grant(
        "jira",
        refresh_field="refresh_token",
        refresh_token="rt-1",
        access_token="at-1",
        issued_at=now,
        expires_at=now + timedelta(hours=1),
        scope=None,
        actor_did="did:arc:test",
    )
    before = await _revision(connections)

    for _ in range(passes):
        assert await connections.renew_credentials() == {"jira": "fresh"}

    assert await _revision(connections) == before
