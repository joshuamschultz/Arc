"""H-024 — arcui connected-data EXPLORER routes (operator-gated, audited).

``GET /api/agents/{agent_id}/knowledge/connected-sources/{source_id}/tables``
and ``.../chunks`` let an operator browse ONE granted connection's datastore
schema and its indexed chunks — bounded to that connection's source-scope, gated
no-read-up, and operator-only. Driven against the real Starlette app
(``arcui.server.create_app``) with a workspace seeded through arcmemory's own
ingest-side primitives — never hand-rolled SQL.

Abuse cases proven here: viewer is 403 on both surfaces; a chunk from a DIFFERENT
connection never appears; an over-clearance (SECRET) chunk never shows and never
shifts the count; a source the agent was never granted reads as empty
(indistinguishable from a nonexistent one — it never confirms another agent's
source ids); vector search degrades LOUD (never a misleading empty).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocIndex
from arcmemory.index.source import SourceChunk
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER_TOKEN = "viewer-tok-explorer"
OPERATOR_TOKEN = "operator-tok-explorer"
_DID = "did:arc:agent:explorer-view"
_SOURCE_A = "acct-a"
_SOURCE_B = "acct-b"


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}


def _operator() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


async def _seed(workspace: Path, datastore_path: Path) -> None:
    cfg = MemoryConfig()
    db = MemoryDB(workspace)
    index = DocIndex(db, workspace, cfg)

    await index.index_source(
        _SOURCE_A,
        _DID,
        [
            SourceChunk(
                chunk_id="a-public",
                source_path=f"{_SOURCE_A}:inv-001",
                text="Acme invoice public total 500",
                classification="unclassified",
                mtime=1.0,
            ),
            SourceChunk(
                chunk_id="a-secret",
                source_path=f"{_SOURCE_A}:inv-secret",
                text="Acme launch codes secret",
                classification="SECRET",
                mtime=2.0,
            ),
        ],
    )
    await index.index_source(
        _SOURCE_B,
        _DID,
        [
            SourceChunk(
                chunk_id="b-only",
                source_path=f"{_SOURCE_B}:ship-1",
                text="Beta corp shipment record",
                classification="unclassified",
                mtime=1.0,
            )
        ],
    )

    brain = ArcMemoryBrain(workspace, _DID)
    conn = sqlite3.connect(str(datastore_path))
    conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, amount TEXT)")
    conn.commit()
    await brain.register_sqlite_datastore(_SOURCE_A, conn)


def _make_agent_dir(team_root: Path, name: str) -> Path:
    agent_dir = team_root / name
    agent_dir.mkdir(parents=True)
    # embed_backend = "none": no query-side embedder, so a ?mode=vector request
    # degrades LOUD through the route (the abuse case) rather than loading weights.
    (agent_dir / "arcagent.toml").write_text(
        f"[agent]\nname = '{name}'\n\n[modules.memory]\nembed_backend = 'none'\n"
    )
    (agent_dir / "workspace").mkdir()
    return agent_dir


def _roster_entry(agent_id: str, did: str, agent_dir: Path) -> RosterEntry:
    return RosterEntry(
        agent_id=agent_id,
        name=agent_id,
        did=did,
        org=None,
        type="agent",
        workspace_path=str(agent_dir),
        model="claude-3-5-sonnet",
        provider="anthropic",
        online=True,
        display_name=agent_id,
        color="#1abc9c",
        role_label="Test",
        hidden=False,
    )


@pytest.fixture
def app_with_explorer_data(tmp_path: Path) -> Iterator[Any]:
    team_root = tmp_path / "team"
    team_root.mkdir()
    agent_dir = _make_agent_dir(team_root, "explorer-agent")
    workspace = agent_dir / "workspace"
    datastore_path = tmp_path / "connected.sqlite"

    import asyncio

    asyncio.run(_seed(workspace, datastore_path))

    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: [_roster_entry("explorer-agent", _DID, agent_dir)]
    yield app


# -- tables -------------------------------------------------------------------


def test_tables_operator_sees_only_this_connections_tables(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            f"/api/agents/explorer-agent/knowledge/connected-sources/{_SOURCE_A}/tables",
            headers=_operator(),
        )
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert items and all(
        item["slug"].startswith(f"db-table-{_SOURCE_A}-") for item in items
    )
    assert any(item["name"] == "invoices" for item in items)


def test_tables_viewer_is_forbidden(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            f"/api/agents/explorer-agent/knowledge/connected-sources/{_SOURCE_A}/tables",
            headers=_viewer(),
        )
    assert resp.status_code == 403


def test_tables_ungranted_source_is_empty_not_confirming(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            "/api/agents/explorer-agent/knowledge/connected-sources/never-granted/tables",
            headers=_operator(),
        )
    assert resp.status_code == 200
    assert resp.json()["items"] == []


# -- chunks -------------------------------------------------------------------


def test_chunks_operator_sees_only_this_connections_chunks(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            f"/api/agents/explorer-agent/knowledge/connected-sources/{_SOURCE_A}/chunks",
            headers=_operator(),
        )
    assert resp.status_code == 200
    body = resp.json()
    texts = " ".join(item["text"] for item in body["items"])
    assert "Acme" in texts
    assert "Beta corp" not in texts  # no cross-connection leak


def test_chunks_over_clearance_never_shows_or_shifts_count(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            f"/api/agents/explorer-agent/knowledge/connected-sources/{_SOURCE_A}/chunks",
            headers=_operator(),
        )
    body = resp.json()
    # The UI carries no clearance credential -> default unclassified: the SECRET
    # chunk is gated out BEFORE the slice, so total reflects only the visible one.
    assert body["total"] == 1
    assert all("launch codes" not in item["text"] for item in body["items"])


def test_chunks_viewer_is_forbidden(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            f"/api/agents/explorer-agent/knowledge/connected-sources/{_SOURCE_A}/chunks",
            headers=_viewer(),
        )
    assert resp.status_code == 403


def test_chunks_search_literal_scoped(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            f"/api/agents/explorer-agent/knowledge/connected-sources/{_SOURCE_A}/chunks?q=Acme",
            headers=_operator(),
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["mode"] == "literal"
    assert body["degraded"] is False
    assert body["items"] and all("Beta corp" not in i["text"] for i in body["items"])


def test_chunks_search_vector_degrades_loud(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            f"/api/agents/explorer-agent/knowledge/connected-sources/{_SOURCE_A}"
            "/chunks?q=Acme&mode=vector",
            headers=_operator(),
        )
    assert resp.status_code == 200
    body = resp.json()
    # No embedder is wired through the file-built operator -> loud degrade, not empty.
    assert body["degraded"] is True
    assert body["mode"] == "literal"


def test_chunks_ungranted_source_is_empty(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            "/api/agents/explorer-agent/knowledge/connected-sources/never-granted/chunks",
            headers=_operator(),
        )
    assert resp.status_code == 200
    assert resp.json()["items"] == []
    assert resp.json()["total"] == 0


def test_unknown_agent_is_404(app_with_explorer_data: Any) -> None:
    with TestClient(app_with_explorer_data) as client:
        resp = client.get(
            f"/api/agents/ghost/knowledge/connected-sources/{_SOURCE_A}/tables",
            headers=_operator(),
        )
    assert resp.status_code == 404
