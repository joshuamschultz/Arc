"""T-708 — Knowledge routes: failing tests first (COMP-002, REQ-084..REQ-100).

Drives the real Starlette app (``arcui.server.create_app``) against a real
fixture memory DB, built through arcmemory's own capture path
(``ArcMemoryBrain.capture`` for episodic entries, ``SemanticStore.write_fact``
for entities — never hand-inserted SQL), mirroring
``packages/arcmemory/tests/unit/test_operator.py``'s fixture pattern.

Covers: viewer reads, operator-gated mutations (403 for viewer), empty-vs-
unreadable store states (REQ-097), and audit emission on every mutation.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from arcmemory.brain import ArcMemoryBrain
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER_TOKEN = "viewer-tok-knowledge"
OPERATOR_TOKEN = "operator-tok-knowledge"
_DID = "did:arc:agent:concierge"
_VOCAB = ["alice", "bob", "carol"]


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}


def _operator() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


async def _seed_episodic(workspace: Path) -> None:
    """Capture episodic memories through the real fast-capture path."""
    brain = ArcMemoryBrain(workspace, _DID, seed_vocabulary=_VOCAB)
    await brain.capture("alice met bob at the summit", kind="observation", salience=0.8)
    await brain.capture("carol reviewed the budget", kind="respond", salience=0.2)
    await brain.capture("the deployment finished cleanly", kind="tool")


def _seed_entities(workspace: Path) -> None:
    """Write entities + a wiki-link edge through the semantic store's own path."""
    store = SemanticStore(workspace, WeightedGraph(MemoryDB(workspace)), scope=_DID)
    store.write_fact("alice", "role", "lead engineer", confidence=0.9)
    store.write_fact("alice", "works-with", "[[bob]]", confidence=0.7)
    store.write_fact("bob", "role", "designer", confidence=0.6)


def _make_agent_dir(team_root: Path, name: str) -> Path:
    agent_dir = team_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(f"[agent]\nname = '{name}'\n")
    (agent_dir / "workspace").mkdir()
    return agent_dir


def _roster(team_root: Path, agent_dir: Path) -> list[RosterEntry]:
    return [
        RosterEntry(
            agent_id="concierge",
            name="concierge",
            did=_DID,
            org=None,
            type="agent",
            workspace_path=str(agent_dir),
            model="claude-3-5-sonnet",
            provider="anthropic",
            online=True,
            display_name="Concierge",
            color="#1abc9c",
            role_label="Test",
            hidden=False,
        )
    ]


@pytest.fixture
def app_with_memories(tmp_path: Path) -> Iterator[Any]:
    """A real app + real seeded memory DB for the 'concierge' agent."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    agent_dir = _make_agent_dir(team_root, "concierge")
    workspace = agent_dir / "workspace"

    import asyncio

    asyncio.run(_seed_episodic(workspace))
    _seed_entities(workspace)

    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: _roster(team_root, agent_dir)
    yield app


@pytest.fixture
def app_no_memories(tmp_path: Path) -> Iterator[Any]:
    """A real app for an agent that has never captured anything (empty state)."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    agent_dir = _make_agent_dir(team_root, "fresh")

    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: [
        RosterEntry(
            agent_id="fresh",
            name="fresh",
            did="did:arc:agent:fresh",
            org=None,
            type="agent",
            workspace_path=str(agent_dir),
            model="claude-3-5-sonnet",
            provider="anthropic",
            online=True,
            display_name="Fresh",
            color="#1abc9c",
            role_label="Test",
            hidden=False,
        )
    ]
    yield app


@pytest.fixture
def app_unreadable(tmp_path: Path) -> Iterator[Any]:
    """An agent whose memory dir is blocked by a file where a dir must be (unreadable)."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    agent_dir = _make_agent_dir(team_root, "broken")
    workspace = agent_dir / "workspace"
    # A plain file at 'memory' — MemoryDB.connect()'s mkdir(parents=True,
    # exist_ok=True) raises FileExistsError against a non-directory here.
    (workspace / "memory").write_text("not a directory")

    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: [
        RosterEntry(
            agent_id="broken",
            name="broken",
            did="did:arc:agent:broken",
            org=None,
            type="agent",
            workspace_path=str(agent_dir),
            model="claude-3-5-sonnet",
            provider="anthropic",
            online=True,
            display_name="Broken",
            color="#1abc9c",
            role_label="Test",
            hidden=False,
        )
    ]
    yield app


# ---------------------------------------------------------------------------
# GET /api/agents/{agent_id}/knowledge/memories — list + metadata (REQ-084)
# ---------------------------------------------------------------------------


class TestListMemories:
    def test_viewer_can_list_paged_with_metadata(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            resp = client.get(
                "/api/agents/concierge/knowledge/memories?limit=2&offset=0", headers=_viewer()
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 3
        assert data["limit"] == 2 and data["offset"] == 0
        assert len(data["items"]) == 2
        record = data["items"][0]
        assert record["entry_id"] and record["text"]
        assert record["created"]
        assert 1 <= record["importance"] <= 10
        assert 0.0 <= record["recency"] <= 1.0
        assert record["source"].endswith(".md")

    def test_unknown_agent_returns_404(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            resp = client.get("/api/agents/ghost/knowledge/memories", headers=_viewer())
        assert resp.status_code == 404
        assert "ghost" in resp.json()["error"]

    def test_no_auth_is_401(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            resp = client.get("/api/agents/concierge/knowledge/memories")
        assert resp.status_code == 401


class TestSearchMemories:
    def test_viewer_search_returns_ranked_hits(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            resp = client.get(
                "/api/agents/concierge/knowledge/memories?q=deployment", headers=_viewer()
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"], "the deployment memory should surface"
        assert any("deployment" in hit["content"] for hit in data["items"])
        scores = [hit["score"] for hit in data["items"]]
        assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------------------
# GET .../memories/{entry_id} + links (REQ-084, REQ-085)
# ---------------------------------------------------------------------------


class TestMemoryDetailAndLinks:
    def test_get_entry_detail(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            listing = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
            entry_id = listing["items"][0]["entry_id"]
            resp = client.get(
                f"/api/agents/concierge/knowledge/memories/{entry_id}", headers=_viewer()
            )
        assert resp.status_code == 200
        assert resp.json()["entry_id"] == entry_id

    def test_get_entry_missing_is_404(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            resp = client.get(
                "/api/agents/concierge/knowledge/memories/does-not-exist", headers=_viewer()
            )
        assert resp.status_code == 404

    def test_memory_links_show_tagged_entities(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            listing = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
            entry = next(r for r in listing["items"] if r["text"] == "alice met bob at the summit")
            resp = client.get(
                f"/api/agents/concierge/knowledge/memories/{entry['entry_id']}/links",
                headers=_viewer(),
            )
        assert resp.status_code == 200
        targets = {link["target_id"] for link in resp.json()["items"]}
        assert {"alice", "bob"} <= targets


# ---------------------------------------------------------------------------
# GET .../entities + detail + links (REQ-084, REQ-085)
# ---------------------------------------------------------------------------


class TestEntities:
    def test_list_entities(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            resp = client.get("/api/agents/concierge/knowledge/entities", headers=_viewer())
        assert resp.status_code == 200
        slugs = {e["slug"] for e in resp.json()["items"]}
        assert slugs == {"alice", "bob"}

    def test_get_entity_detail(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            resp = client.get("/api/agents/concierge/knowledge/entities/alice", headers=_viewer())
        assert resp.status_code == 200
        assert resp.json()["slug"] == "alice"
        # Entity-level confidence is the frontmatter default (0.5) — write_fact
        # sets the *fact*'s confidence, not the entity's (see arcmemory's own
        # test_operator.py::test_list_entities_returns_typed_records).
        assert resp.json()["importance"] == 5

    def test_get_entity_missing_is_404(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            resp = client.get(
                "/api/agents/concierge/knowledge/entities/ghost-slug", headers=_viewer()
            )
        assert resp.status_code == 404

    def test_entity_links_expose_wiki_edges(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            resp = client.get(
                "/api/agents/concierge/knowledge/entities/alice/links", headers=_viewer()
            )
        assert resp.status_code == 200
        targets = {link["target_id"] for link in resp.json()["items"]}
        assert "bob" in targets


# ---------------------------------------------------------------------------
# PATCH / DELETE mutations — operator-gated, audited (REQ-088, REQ-089, REQ-100)
# ---------------------------------------------------------------------------


class TestMutations:
    def test_viewer_patch_is_403(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            listing = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
            entry_id = listing["items"][0]["entry_id"]
            resp = client.patch(
                f"/api/agents/concierge/knowledge/memories/{entry_id}",
                json={"text": "hacked"},
                headers=_viewer(),
            )
        assert resp.status_code == 403

    def test_viewer_delete_is_403(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            listing = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
            entry_id = listing["items"][0]["entry_id"]
            resp = client.delete(
                f"/api/agents/concierge/knowledge/memories/{entry_id}", headers=_viewer()
            )
        assert resp.status_code == 403

    def test_operator_can_edit_text(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            listing = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
            entry_id = listing["items"][0]["entry_id"]
            resp = client.patch(
                f"/api/agents/concierge/knowledge/memories/{entry_id}",
                json={"text": "corrected text"},
                headers=_operator(),
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "applied"

            detail = client.get(
                f"/api/agents/concierge/knowledge/memories/{entry_id}", headers=_viewer()
            ).json()
        assert detail["text"] == "corrected text"

    def test_operator_can_set_metadata(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            listing = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
            entry = next(r for r in listing["items"] if r["text"] == "carol reviewed the budget")
            resp = client.patch(
                f"/api/agents/concierge/knowledge/memories/{entry['entry_id']}",
                json={"importance": 9},
                headers=_operator(),
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "applied"

            detail = client.get(
                f"/api/agents/concierge/knowledge/memories/{entry['entry_id']}",
                headers=_viewer(),
            ).json()
        assert detail["importance"] == 9

    def test_patch_with_no_fields_is_400(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            listing = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
            entry_id = listing["items"][0]["entry_id"]
            resp = client.patch(
                f"/api/agents/concierge/knowledge/memories/{entry_id}",
                json={},
                headers=_operator(),
            )
        assert resp.status_code == 400

    def test_operator_can_delete(self, app_with_memories: Any) -> None:
        with TestClient(app_with_memories) as client:
            listing = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
            entry_id = listing["items"][0]["entry_id"]
            resp = client.delete(
                f"/api/agents/concierge/knowledge/memories/{entry_id}", headers=_operator()
            )
            assert resp.status_code == 200
            assert resp.json()["status"] == "applied"

            after = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
        assert after["total"] == 2

    def test_mutation_on_missing_entry_is_error_not_partial(self, app_with_memories: Any) -> None:
        """REQ-089 — the error surfaces verbatim; no partial success."""
        with TestClient(app_with_memories) as client:
            resp = client.patch(
                "/api/agents/concierge/knowledge/memories/ghost-id",
                json={"text": "x"},
                headers=_operator(),
            )
        assert resp.status_code == 404
        body = resp.json()
        assert body["status"] == "error"
        assert "ghost-id" in body["results"][0]["error"]

    def test_mutation_emits_audit_event(
        self, app_with_memories: Any, caplog: pytest.LogCaptureFixture
    ) -> None:
        with TestClient(app_with_memories) as client:
            listing = client.get(
                "/api/agents/concierge/knowledge/memories?limit=50", headers=_viewer()
            ).json()
            entry_id = listing["items"][0]["entry_id"]
            with caplog.at_level("INFO", logger="arcui.audit"):
                resp = client.patch(
                    f"/api/agents/concierge/knowledge/memories/{entry_id}",
                    json={"text": "audited edit"},
                    headers=_operator(),
                )
        assert resp.status_code == 200

        mutations = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert len(mutations) == 1
        details = mutations[0]["details"]
        assert details["actor_role"] == "operator"
        assert details["operation"] == "memory.edit"
        assert details["outcome"] == "applied"
        assert entry_id in details["target"]


# ---------------------------------------------------------------------------
# Empty vs unreadable store (REQ-097)
# ---------------------------------------------------------------------------


class TestEmptyVsUnreadable:
    def test_no_memories_recorded_is_200_empty(self, app_no_memories: Any) -> None:
        with TestClient(app_no_memories) as client:
            resp = client.get("/api/agents/fresh/knowledge/memories", headers=_viewer())
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_unreadable_store_is_503_with_verbatim_error(self, app_unreadable: Any) -> None:
        with TestClient(app_unreadable) as client:
            resp = client.get("/api/agents/broken/knowledge/memories", headers=_viewer())
        assert resp.status_code == 503
        assert resp.json()["error"]  # arcmemory's exception message, surfaced verbatim
