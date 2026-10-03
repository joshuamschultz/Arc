"""Hotfix: an OAuth connection with no grant is "Needs you -> Reconnect", not an error.

DGX 9c280994: Confluence (OAuth, never signed in after the custody move) showed
status ``error`` / ``provider_unavailable`` / action ``wait`` while its own text
said "click Connect". The probe's free text carried no reason code, so the
classifier fell through to ``provider_unavailable``. An OAuth connection whose
custody row holds no grant is classified before any provider call.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust.connector_cipher import ConnectorSecretCipher

import arcagent
from arcagent.extension.grants import Connection

REPO_EXTENSIONS = Path(__file__).resolve().parents[5] / "extensions"
CIPHER = ConnectorSecretCipher(b"\x0d" * 32)
PROBE_DID = "did:arc:system:health-probe"


def _connections(tmp_path: Path, extension: str) -> arcagent.Connections:
    backend = FakeBackend()

    async def opener() -> Any:
        return backend

    world = arcagent.resolve_deployment(arc_dir=tmp_path / "arc", extensions_root=REPO_EXTENSIONS)
    connections = arcagent.Connections(world, state_opener=opener, credential_cipher=CIPHER)
    connections.registry.define(extension, Connection(extension=extension, approval="auto"))
    return connections


@pytest.mark.parametrize("extension", ["confluence", "jira", "dropbox", "google_workspace"])
async def test_oauth_connection_without_a_grant_needs_you_reconnect(
    tmp_path: Path, extension: str
) -> None:
    connections = _connections(tmp_path, extension)

    for _ in range(4):
        record = await connections.check_health(extension, checked_by=PROBE_DID)

    assert (record.status, record.reason_code, record.action) == (
        "needs_you",
        "credential_missing",
        "reconnect",
    )
