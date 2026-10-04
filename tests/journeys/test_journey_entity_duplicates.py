"""Journey: one real thing filed twice becomes one card (operator items 6 and 7).

The agent's memory holds "Harness Advantage" twice: once filed as a document,
once as a thesis, each with its own facts and type words in its tags. The
operator opens the Entities view, sees the pair under "Review duplicates" and
presses "Merge". Afterwards there is ONE card of type thesis, its tags carry no
"thesis"/"document", both cards' facts are on it, and the old id still opens it.

Real deployment (operator key, minted agent DID, real roster), the real arcui
knowledge routes behind the real auth middleware, the real arcmemory store on
disk. No model is involved: the person's "Merge" is the confirmation.
"""

from __future__ import annotations

from typing import Any

from arcgateway import team_roster
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.knowledge import routes as knowledge_routes
from starlette.applications import Starlette
from starlette.testclient import TestClient

from .conftest import OPERATOR_TOKEN, VIEWER_TOKEN, Deployment


def _knowledge_ui(deployment: Deployment) -> TestClient:
    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = Starlette(routes=knowledge_routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.roster_provider = lambda: team_roster.list_team(
        team_root=deployment.team_root, online_ids=set()
    )
    client = TestClient(app)
    client.headers.update({"Authorization": f"Bearer {OPERATOR_TOKEN}"})
    return client


def _agent(deployment: Deployment) -> Any:
    [entry] = team_roster.list_team(team_root=deployment.team_root, online_ids=set())
    return entry


def test_two_cards_for_one_thesis_become_one_thesis_card(deployment: Deployment) -> None:
    config = deployment.agent_dir / "arcagent.toml"
    config.write_text(
        config.read_text(encoding="utf-8") + "\n[modules.memory]\nembed_backend = 'none'\n",
        encoding="utf-8",
    )
    entry = _agent(deployment)
    workspace = deployment.agent_dir / "workspace"
    store = SemanticStore(workspace, WeightedGraph(MemoryDB(workspace)), scope=entry.did)
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
    base = f"/api/agents/{entry.agent_id}/knowledge/entities"

    with _knowledge_ui(deployment) as ui:
        listing = ui.get(f"{base}/duplicates")
        assert listing.status_code == 200, listing.text
        [proposal] = listing.json()["items"]
        assert proposal["entity_type"] == "thesis"

        merged = ui.post(f"{base}/duplicates/merge", json={"slugs": proposal["slugs"]})
        assert merged.status_code == 200, merged.text
        survivor = merged.json()["survivor"]

        entities = ui.get(base).json()["items"]
        card = ui.get(f"{base}/{survivor}").json()
        folded = (set(proposal["slugs"]) - {survivor}).pop()
        redirected = ui.get(f"{base}/{folded}").json()
        after = ui.get(f"{base}/duplicates").json()["items"]

    assert [e["slug"] for e in entities if e["name"] == "Harness Advantage"] == [survivor]
    assert card["entity_type"] == "thesis"
    assert not {"thesis", "document"} & {t.casefold() for t in card["tags"]}
    assert sorted(card["tags"]) == ["arc", "federal-sales"]
    facts = " ".join(card["facts"])
    assert "the harness is the moat" in facts and "three customers chose the harness" in facts
    assert redirected["slug"] == survivor
    assert after == []
