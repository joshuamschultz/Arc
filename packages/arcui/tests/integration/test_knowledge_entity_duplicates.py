"""Review duplicates — ``/api/agents/{id}/knowledge/entities/duplicates[/merge|/reject]``.

Drives the real Starlette app over a real agent workspace written through
arcmemory's own store. The operator sees two cards for one thing (one filed as a
document, one as a thesis), presses "Merge", and gets one thesis card holding both
cards' facts; the old id redirects. "Not the same" is remembered. Viewers can read
the proposals but never act on them, and every action is audited.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER = {"Authorization": "Bearer viewer-tok-dupes"}
OPERATOR = {"Authorization": "Bearer operator-tok-dupes"}
_DID = "did:arc:agent:olivia"
_BASE = "/api/agents/olivia/knowledge/entities"


def _seed(workspace: Path) -> SemanticStore:
    store = SemanticStore(workspace, WeightedGraph(MemoryDB(workspace)), scope=_DID)
    store.write_fact(
        "harness-advantage",
        "claim",
        "the harness is the moat",
        name="Harness Advantage",
        entity_type="document",
        tags=["document", "arc"],
    )
    store.write_fact(
        "harness-advantage-thesis",
        "evidence",
        "three customers chose the harness",
        name="Harness Advantage",
        entity_type="thesis",
        tags=["thesis", "federal-sales"],
    )
    store.write_fact("brad", "role", "cto", name="Brad Baker", entity_type="person")
    store.write_fact("brad-b", "city", "austin", name="Brad Baker", entity_type="person")
    return store


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Any]:
    team_root = tmp_path / "team"
    agent_dir = team_root / "olivia"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        "[agent]\nname = 'olivia'\n\n[modules.memory]\nembed_backend = 'none'\n"
    )
    _seed(agent_dir / "workspace")
    auth = AuthConfig({"viewer_token": "viewer-tok-dupes", "operator_token": "operator-tok-dupes"})
    application = create_app(team_root=team_root, auth_config=auth)
    application.state.roster_provider = lambda: [
        RosterEntry(
            agent_id="olivia",
            name="olivia",
            did=_DID,
            org=None,
            type="agent",
            workspace_path=str(agent_dir),
            model="m",
            provider="p",
            online=True,
            display_name="Olivia",
            color="#1abc9c",
            role_label="Test",
            hidden=False,
        )
    ]
    application.state.test_workspace = agent_dir / "workspace"
    yield application


def _proposal_sets(client: TestClient) -> set[frozenset[str]]:
    resp = client.get(f"{_BASE}/duplicates", headers=VIEWER)
    assert resp.status_code == 200
    return {frozenset(p["slugs"]) for p in resp.json()["items"]}


def test_viewer_sees_proposals_with_the_type_the_merge_would_keep(app: Any) -> None:
    with TestClient(app) as client:
        resp = client.get(f"{_BASE}/duplicates", headers=VIEWER)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    by_slugs = {frozenset(p["slugs"]): p for p in body["items"]}
    harness = by_slugs[frozenset({"harness-advantage", "harness-advantage-thesis"})]
    assert harness["entity_type"] == "thesis"
    assert {c["entity_type"] for c in harness["cards"]} == {"document", "thesis"}
    assert frozenset({"brad", "brad-b"}) in by_slugs


def test_merge_makes_one_thesis_card_with_both_cards_facts(
    app: Any, caplog: pytest.LogCaptureFixture
) -> None:
    with TestClient(app) as client:
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post(
                f"{_BASE}/duplicates/merge",
                json={"slugs": ["harness-advantage", "harness-advantage-thesis"]},
                headers=OPERATOR,
            )
        assert resp.status_code == 200
        survivor = resp.json()["survivor"]
        card = client.get(f"{_BASE}/{survivor}", headers=VIEWER).json()
        folded = ({"harness-advantage", "harness-advantage-thesis"} - {survivor}).pop()
        redirected = client.get(f"{_BASE}/{folded}", headers=VIEWER).json()
        remaining = _proposal_sets(client)

    assert card["entity_type"] == "thesis"
    assert sorted(card["tags"]) == ["arc", "federal-sales"]
    assert {f.split(":")[0].lstrip("- ") for f in card["facts"]} == {"claim", "evidence"}
    assert redirected["slug"] == survivor
    assert remaining == {frozenset({"brad", "brad-b"})}
    audits = [
        json.loads(r.message)["details"]
        for r in caplog.records
        if r.name == "arcui.audit" and '"ui.mutation"' in r.message
    ]
    assert [a["operation"] for a in audits] == ["memory.entity_merge"]
    assert audits[0]["outcome"] == "applied"


def test_not_the_same_is_remembered(app: Any) -> None:
    with TestClient(app) as client:
        resp = client.post(
            f"{_BASE}/duplicates/reject", json={"slugs": ["brad", "brad-b"]}, headers=OPERATOR
        )
        assert resp.status_code == 200
        assert frozenset({"brad", "brad-b"}) not in _proposal_sets(client)

    with TestClient(app) as client:  # a fresh process reads the same decision
        assert frozenset({"brad", "brad-b"}) not in _proposal_sets(client)
    store = SemanticStore(
        app.state.test_workspace,
        WeightedGraph(MemoryDB(app.state.test_workspace)),
        scope=_DID,
    )
    assert {"brad", "brad-b"} <= set(store.slugs())


@pytest.mark.parametrize("action", ["merge", "reject"])
def test_viewer_cannot_act(app: Any, action: str) -> None:
    with TestClient(app) as client:
        resp = client.post(
            f"{_BASE}/duplicates/{action}", json={"slugs": ["brad", "brad-b"]}, headers=VIEWER
        )
    assert resp.status_code == 403


@pytest.mark.parametrize(
    "body", [{}, {"slugs": "brad"}, {"slugs": ["brad"]}, {"slugs": ["brad", 3]}]
)
def test_malformed_requests_are_400(app: Any, body: dict[str, Any]) -> None:
    with TestClient(app) as client:
        resp = client.post(f"{_BASE}/duplicates/merge", json=body, headers=OPERATOR)
    assert resp.status_code == 400


def test_a_merge_the_guards_refuse_is_409_and_changes_nothing(app: Any) -> None:
    store = SemanticStore(
        app.state.test_workspace,
        WeightedGraph(MemoryDB(app.state.test_workspace)),
        scope=_DID,
    )
    store.write_fact("austin", "p", "v", name="Austin", entity_type="place")
    before = sorted(store.slugs())
    with TestClient(app) as client:
        resp = client.post(
            f"{_BASE}/duplicates/merge", json={"slugs": ["austin", "brad"]}, headers=OPERATOR
        )
    assert resp.status_code == 409
    assert sorted(store.slugs()) == before
