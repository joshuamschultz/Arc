"""RED — SPEC-073 Phase A (A2): arcui connector-data view routes.

None of these ``GET /api/agents/{agent_id}/knowledge/{sources,mappings,
blob-folders,datastore-tables,documents,datastore,provenance,index-health,
sources/{id}/mapping}`` routes are registered yet in
``arcui.routes.knowledge`` -- every request below is expected to 404
against the real Starlette app (``arcui.server.create_app``), not because
the agent/store is missing but because the ROUTE itself does not exist.

Mirrors ``test_knowledge_routes.py``'s fixture pattern: a real app, a real
roster entry, and a workspace seeded through arcmemory's own ingest-side
primitives (``ingest.register_source``, ``mapping.commit_mapping``,
``blob_ontology.walk_blob_source``, ``DocIndex.index_source``,
``ProvenanceStore.record``, ``ArcMemoryBrain.register_datastore``) -- never
hand-rolled SQL, never a mock of arcmemory's own stores.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from arcmemory import ingest
from arcmemory.blob_ontology import BlobObject, walk_blob_source
from arcmemory.brain import ArcMemoryBrain
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.doc_index import DocIndex
from arcmemory.index.graph import WeightedGraph
from arcmemory.index.source import SourceChunk
from arcmemory.mapping import commit_mapping
from arcmemory.stores.provenance import ProvenanceStore
from arcmemory.stores.semantic import SemanticStore
from arcmemory.types import Provenance, Scope, SourceMapping
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER_TOKEN = "viewer-tok-connectors"
_DID = "did:arc:agent:connector-view"
_SOURCE_ID = "acct-1"
_ITEM_ID = "canonical-item-1"


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}


def _scope() -> Scope:
    return Scope(agent_did=_DID)


async def _seed_connector_data(workspace: Path, datastore_path: Path) -> None:
    """Seed one connected source end to end through the real ingest side."""
    graph = WeightedGraph(MemoryDB(workspace))
    scope = _scope()

    ingest.register_source(workspace, graph, scope, _SOURCE_ID, kind="quickbooks")

    store = SemanticStore(workspace, graph, scope.key)
    commit_mapping(
        SourceMapping(source_id=_SOURCE_ID, homes=["document", "datastore"]), store=store
    )

    walk_blob_source(
        [BlobObject(path="invoices/2026-01.pdf", mime="application/pdf", size=1024)],
        source_id=_SOURCE_ID,
        store=store,
    )

    cfg = MemoryConfig()
    db = MemoryDB(workspace)
    await DocIndex(db, workspace, cfg).index_source(
        _SOURCE_ID,
        _DID,
        [
            SourceChunk(
                chunk_id="chunk-inv-001",
                source_path=f"{_SOURCE_ID}:inv-001",
                text="Invoice 001 for Acme Corp, amount 500",
                classification="unclassified",
                mtime=0.0,
            )
        ],
    )

    ProvenanceStore(db).record(
        _ITEM_ID,
        Provenance(source=_SOURCE_ID, external_id="inv-001", classification="unclassified"),
    )
    ProvenanceStore(db).record(
        _ITEM_ID,
        Provenance(source="other-source", external_id="ext-9", classification="unclassified"),
    )

    brain = ArcMemoryBrain(workspace, _DID)
    conn = sqlite3.connect(str(datastore_path))
    conn.execute("CREATE TABLE invoices (id TEXT PRIMARY KEY, amount TEXT)")
    conn.execute("INSERT INTO invoices (id, amount) VALUES ('001', '500')")
    conn.commit()
    await brain.register_sqlite_datastore(_SOURCE_ID, conn)


def _make_agent_dir(team_root: Path, name: str) -> Path:
    agent_dir = team_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(f"[agent]\nname = '{name}'\n")
    (agent_dir / "workspace").mkdir()
    return agent_dir


def _roster_entry(agent_id: str, did: str, agent_dir: Path, *, display_name: str) -> RosterEntry:
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
        display_name=display_name,
        color="#1abc9c",
        role_label="Test",
        hidden=False,
    )


@pytest.fixture
def app_with_connector_data(tmp_path: Path) -> Iterator[Any]:
    """A real app + a real workspace seeded with one connected source."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    agent_dir = _make_agent_dir(team_root, "connector-agent")
    workspace = agent_dir / "workspace"
    datastore_path = tmp_path / "connected-accounting.sqlite"

    import asyncio

    asyncio.run(_seed_connector_data(workspace, datastore_path))

    auth = AuthConfig({"viewer_token": VIEWER_TOKEN})
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: [
        _roster_entry("connector-agent", _DID, agent_dir, display_name="Connector Agent")
    ]
    yield app


@pytest.fixture
def app_no_connector_data(tmp_path: Path) -> Iterator[Any]:
    """A real app for an agent that has never registered a connected source (empty state)."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    agent_dir = _make_agent_dir(team_root, "fresh")

    auth = AuthConfig({"viewer_token": VIEWER_TOKEN})
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: [
        _roster_entry("fresh", "did:arc:agent:fresh", agent_dir, display_name="Fresh")
    ]
    yield app


# ---------------------------------------------------------------------------
# GET .../knowledge/sources + .../sources/{id}/mapping + .../mappings
# ---------------------------------------------------------------------------


class TestSourcesAndMappings:
    def test_list_sources_returns_the_registered_source(
        self, app_with_connector_data: Any
    ) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get("/api/agents/connector-agent/knowledge/sources", headers=_viewer())
        assert resp.status_code == 200
        slugs = {item["slug"] for item in resp.json()["items"]}
        assert f"source-{_SOURCE_ID}" in slugs

    def test_get_source_mapping_returns_the_committed_homes(
        self, app_with_connector_data: Any
    ) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get(
                f"/api/agents/connector-agent/knowledge/sources/{_SOURCE_ID}/mapping",
                headers=_viewer(),
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["item"]["source_id"] == _SOURCE_ID
        assert set(body["item"]["homes"]) == {"document", "datastore"}

    def test_list_mappings_returns_the_committed_mapping(
        self, app_with_connector_data: Any
    ) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get("/api/agents/connector-agent/knowledge/mappings", headers=_viewer())
        assert resp.status_code == 200
        source_ids = {item["source_id"] for item in resp.json()["items"]}
        assert _SOURCE_ID in source_ids

    def test_list_sources_empty_workspace_is_200_empty(self, app_no_connector_data: Any) -> None:
        with TestClient(app_no_connector_data) as client:
            resp = client.get("/api/agents/fresh/knowledge/sources", headers=_viewer())
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# GET .../knowledge/blob-folders
# ---------------------------------------------------------------------------


class TestBlobFolders:
    def test_list_blob_folders_returns_the_walked_folder(
        self, app_with_connector_data: Any
    ) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get(
                "/api/agents/connector-agent/knowledge/blob-folders", headers=_viewer()
            )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items and all(item["entity_type"] == "blob_folder" for item in items)

    def test_list_blob_folders_scoped_by_source_query_param(
        self, app_with_connector_data: Any
    ) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get(
                f"/api/agents/connector-agent/knowledge/blob-folders?source={_SOURCE_ID}",
                headers=_viewer(),
            )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items and all(item["slug"].startswith(f"blob-{_SOURCE_ID}-") for item in items)

    def test_list_blob_folders_empty_workspace_is_200_empty(
        self, app_no_connector_data: Any
    ) -> None:
        with TestClient(app_no_connector_data) as client:
            resp = client.get("/api/agents/fresh/knowledge/blob-folders", headers=_viewer())
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# GET .../knowledge/datastore-tables + .../datastore
# ---------------------------------------------------------------------------


class TestDatastore:
    def test_list_datastore_tables_returns_the_introspected_table(
        self, app_with_connector_data: Any
    ) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get(
                "/api/agents/connector-agent/knowledge/datastore-tables", headers=_viewer()
            )
        assert resp.status_code == 200
        slugs = {item["slug"] for item in resp.json()["items"]}
        assert "db-table-invoices" in slugs

    def test_datastore_query_returns_the_reopened_row(self, app_with_connector_data: Any) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get(
                "/api/agents/connector-agent/knowledge/datastore"
                f"?source={_SOURCE_ID}&op=get_record&table=invoices&pk_value=001",
                headers=_viewer(),
            )
        assert resp.status_code == 200
        row = resp.json()["result"]
        assert row["id"] == "001"
        assert row["amount"] == "500"

    def test_list_datastore_tables_empty_workspace_is_200_empty(
        self, app_no_connector_data: Any
    ) -> None:
        with TestClient(app_no_connector_data) as client:
            resp = client.get("/api/agents/fresh/knowledge/datastore-tables", headers=_viewer())
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# GET .../knowledge/documents
# ---------------------------------------------------------------------------


class TestDocuments:
    def test_document_search_finds_the_indexed_chunk(self, app_with_connector_data: Any) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get(
                f"/api/agents/connector-agent/knowledge/documents?source={_SOURCE_ID}&q=Acme",
                headers=_viewer(),
            )
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert items and any("Acme" in item["text"] for item in items)

    def test_document_search_empty_workspace_is_200_empty(
        self, app_no_connector_data: Any
    ) -> None:
        with TestClient(app_no_connector_data) as client:
            resp = client.get(
                "/api/agents/fresh/knowledge/documents?source=none&q=anything",
                headers=_viewer(),
            )
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# GET .../knowledge/provenance/{item_id}
# ---------------------------------------------------------------------------


class TestProvenance:
    def test_provenance_returns_both_recorded_sources(self, app_with_connector_data: Any) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get(
                f"/api/agents/connector-agent/knowledge/provenance/{_ITEM_ID}",
                headers=_viewer(),
            )
        assert resp.status_code == 200
        sources = {item["source"] for item in resp.json()["items"]}
        assert sources == {_SOURCE_ID, "other-source"}

    def test_provenance_unknown_item_is_200_empty(self, app_with_connector_data: Any) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get(
                "/api/agents/connector-agent/knowledge/provenance/never-recorded",
                headers=_viewer(),
            )
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# GET .../knowledge/index-health
# ---------------------------------------------------------------------------


class TestIndexHealth:
    def test_index_health_returns_a_semantic_status(self, app_with_connector_data: Any) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get(
                "/api/agents/connector-agent/knowledge/index-health", headers=_viewer()
            )
        assert resp.status_code == 200
        body = resp.json()["item"]
        assert "embedder_live" in body
        assert "vec_extension" in body

    def test_index_health_empty_workspace_is_200(self, app_no_connector_data: Any) -> None:
        with TestClient(app_no_connector_data) as client:
            resp = client.get("/api/agents/fresh/knowledge/index-health", headers=_viewer())
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Cross-cutting: unknown agent + no auth
# ---------------------------------------------------------------------------


class TestUnknownAgentAndAuth:
    def test_sources_unknown_agent_is_404(self, app_with_connector_data: Any) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get("/api/agents/ghost/knowledge/sources", headers=_viewer())
        assert resp.status_code == 404

    def test_sources_no_auth_is_401(self, app_with_connector_data: Any) -> None:
        with TestClient(app_with_connector_data) as client:
            resp = client.get("/api/agents/connector-agent/knowledge/sources")
        assert resp.status_code == 401
