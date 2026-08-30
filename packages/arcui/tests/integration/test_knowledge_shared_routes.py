"""H-027 — the fleet shared-knowledge READ view (``/api/team/knowledge/shared``).

Drives the real Starlette app against a real promoted collection built through
``FleetSharedKnowledgeService`` (never hand-written files), proving the view is
access-scoped, classification-filtered, revocation-aware, and attributes each
promotion to the owning agent's roster display name.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from arcmemory.adapters.personal_knowledge import PersonalKnowledgeAdapter
from arctrust import AgentIdentity
from starlette.testclient import TestClient

from arcteam.shared_knowledge import FleetSharedKnowledgeService
from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER_TOKEN = "viewer-tok-shared"
OPERATOR_TOKEN = "operator-tok-shared"


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}


class _Access:
    def __init__(self, identity: AgentIdentity, clearance: str = "UNCLASSIFIED") -> None:
        self.caller_did = identity.did
        self.clearance = clearance


class _Draft:
    def __init__(self, title: str, classification: str = "UNCLASSIFIED") -> None:
        self.title = title
        self.content = f"Body of {title}."
        self.classification = classification
        self.tags = ("release",)
        self.document_type = "procedure"


async def _promote(service: Any, tmp_path: Path, identity: AgentIdentity, draft: _Draft) -> Any:
    access = _Access(identity, draft.classification)
    personal = PersonalKnowledgeAdapter(tmp_path / f"personal-{identity.did[-8:]}", identity.did)
    ref = await personal.save(draft, access)
    return await service.promote(personal, ref.identifier, access, identity)


@pytest.fixture
def app_with_shared(tmp_path: Path) -> Iterator[tuple[Any, dict[str, str], dict[str, str]]]:
    """App + a real fleet collection: alpha (unclassified) and bravo (secret)."""
    import asyncio

    team_root = tmp_path / "team"
    team_root.mkdir()
    alpha = AgentIdentity.generate("test", "alpha")
    bravo = AgentIdentity.generate("test", "bravo")

    service = FleetSharedKnowledgeService.for_team_root(team_root)
    refs: dict[str, str] = {}

    async def _seed() -> None:
        refs["alpha"] = (
            await _promote(service, tmp_path, alpha, _Draft("Alpha runbook"))
        ).identifier
        refs["bravo"] = (
            await _promote(service, tmp_path, bravo, _Draft("Bravo secret", "SECRET"))
        ).identifier

    asyncio.run(_seed())

    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: [
        RosterEntry(
            agent_id="alpha",
            name="alpha",
            did=alpha.did,
            org=None,
            type="agent",
            workspace_path=str(tmp_path),
            model="m",
            provider="p",
            online=True,
            display_name="Alpha",
            color="#111111",
            role_label="",
            hidden=False,
        ),
        RosterEntry(
            agent_id="bravo",
            name="bravo",
            did=bravo.did,
            org=None,
            type="agent",
            workspace_path=str(tmp_path),
            model="m",
            provider="p",
            online=True,
            display_name="Bravo",
            color="#222222",
            role_label="",
            hidden=False,
        ),
    ]
    yield app, {"alpha": alpha.did, "bravo": bravo.did}, refs


def test_list_shows_unclassified_promotions_attributed_to_the_owner(app_with_shared) -> None:
    app, dids, _refs = app_with_shared
    client = TestClient(app)
    resp = client.get("/api/team/knowledge/shared", headers=_viewer())
    assert resp.status_code == 200, resp.text
    docs = resp.json()["documents"]
    by_title = {d["title"]: d for d in docs}
    # UNCLASSIFIED promotion is visible and carries its owning agent's display name.
    assert "Alpha runbook" in by_title
    assert by_title["Alpha runbook"]["owner_did"] == dids["alpha"]
    assert by_title["Alpha runbook"]["owner_display"] == "Alpha"
    # no-read-up hides the SECRET promotion from the UNCLASSIFIED dashboard view.
    assert "Bravo secret" not in by_title


def test_search_is_classification_filtered(app_with_shared) -> None:
    app, _dids, _refs = app_with_shared
    client = TestClient(app)
    hits = client.get("/api/team/knowledge/shared/search?q=Body", headers=_viewer()).json()["hits"]
    titles = {h["title"] for h in hits}
    assert "Alpha runbook" in titles
    assert "Bravo secret" not in titles


def test_read_one_document_and_hide_a_document_above_clearance(app_with_shared) -> None:
    app, _dids, refs = app_with_shared
    client = TestClient(app)
    ok = client.get(f"/api/team/knowledge/shared/{refs['alpha']}", headers=_viewer())
    assert ok.status_code == 200, ok.text
    assert ok.json()["content"] == "Body of Alpha runbook."
    # The SECRET doc exists but is not readable at UNCLASSIFIED clearance.
    blocked = client.get(f"/api/team/knowledge/shared/{refs['bravo']}", headers=_viewer())
    assert blocked.status_code == 403


def test_empty_when_no_team_root(tmp_path: Path) -> None:
    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = create_app(auth_config=auth)
    app.state.team_root = None
    client = TestClient(app)
    resp = client.get("/api/team/knowledge/shared", headers=_viewer())
    assert resp.status_code == 200
    assert resp.json() == {"documents": []}
