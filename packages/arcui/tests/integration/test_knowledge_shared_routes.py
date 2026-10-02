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
from arcteam.shared_knowledge import FleetSharedKnowledgeService
from arctrust import AgentIdentity
from arctrust.identity import did_from_public_key
from arctrust.signer import InProcessSigner
from starlette.testclient import TestClient

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
    assert resp.json() == {
        "documents": [],
        "counts": {"all": 0, "insight": 0, "procedure": 0, "entity": 0, "demoted": 0},
    }


# ---------------------------------------------------------------------------
# Alpha-2 item 16 — typed listing, provenance, operator demote
# ---------------------------------------------------------------------------

_OPERATOR_SIGNER = InProcessSigner(b"\x0a" * 32)
_OPERATOR_DID = did_from_public_key(
    _OPERATOR_SIGNER.public_key, org="operator", agent_type="approver"
)


def _operator() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


class _RecordingWorm:
    """Duck-types MutationWormWriter: captures every mutation audit record."""

    def __init__(self) -> None:
        self.fields: list[Any] = []

    def write(self, fields: Any) -> None:
        self.fields.append(fields)

    def records(self, operation: str) -> list[dict[str, Any]]:
        dumped = [item.model_dump(mode="json") for item in self.fields]
        return [record for record in dumped if record.get("operation") == operation]


def _with_operator(app: Any, *, signer: Any = _OPERATOR_SIGNER) -> _RecordingWorm:
    app.state.operator_signer_factory = lambda: signer
    worm = _RecordingWorm()
    app.state.audit_worm = worm
    return worm


def test_kind_and_provenance_fields(app_with_shared) -> None:
    app, dids, refs = app_with_shared
    client = TestClient(app)

    listing = client.get("/api/team/knowledge/shared", headers=_viewer()).json()
    detail = client.get(f"/api/team/knowledge/shared/{refs['alpha']}", headers=_viewer()).json()

    [doc] = listing["documents"]
    assert doc["kind"] == "procedure"
    assert doc["contributors"] == [{"did": dids["alpha"], "display": "Alpha"}]
    assert doc["promoted_at"]
    assert doc["demotion"] is None
    assert listing["counts"] == {
        "all": 1,
        "insight": 0,
        "procedure": 1,
        "entity": 0,
        "demoted": 0,
    }
    assert detail["kind"] == "procedure"
    [provenance] = detail["provenance"]
    assert provenance["contributor_did"] == dids["alpha"]
    assert provenance["contributor_display"] == "Alpha"
    assert provenance["decision"] == "direct"
    assert provenance["promoted_at"] == detail["promoted_at"]


def test_kind_filter_and_bad_kind(app_with_shared) -> None:
    app, _dids, _refs = app_with_shared
    client = TestClient(app)

    insights = client.get("/api/team/knowledge/shared?kind=insight", headers=_viewer())
    bad = client.get("/api/team/knowledge/shared?kind=fact", headers=_viewer())

    assert insights.json()["documents"] == []
    assert insights.json()["counts"]["procedure"] == 1
    assert bad.status_code == 422


def test_demote_role_gated_and_audited(app_with_shared) -> None:
    app, _dids, refs = app_with_shared
    worm = _with_operator(app)
    client = TestClient(app)
    url = f"/api/team/knowledge/shared/{refs['alpha']}/demote"

    viewer = client.post(url, json={"reason": "stale"}, headers=_viewer())
    operator = client.post(url, json={"reason": "stale"}, headers=_operator())

    assert viewer.status_code == 403
    assert operator.status_code == 200, operator.text
    assert operator.json() == {
        "identifier": refs["alpha"],
        "demoted_by": _OPERATOR_DID,
        "reason": "stale",
        "demoted_at": operator.json()["demoted_at"],
    }
    assert [r["outcome"] for r in worm.records("knowledge.demote")] == ["denied", "applied"]
    listing = client.get("/api/team/knowledge/shared", headers=_viewer()).json()
    assert listing["documents"] == []
    shown = client.get("/api/team/knowledge/shared?include_demoted=1", headers=_viewer()).json()
    [demoted] = shown["documents"]
    assert demoted["demotion"]["demoted_by"] == _OPERATOR_DID
    assert shown["counts"]["demoted"] == 1
    gone = client.get(f"/api/team/knowledge/shared/{refs['alpha']}", headers=_viewer())
    assert gone.status_code == 404


def test_demote_refusals(app_with_shared) -> None:
    app, _dids, refs = app_with_shared
    worm = _with_operator(app)
    client = TestClient(app)
    url = f"/api/team/knowledge/shared/{refs['alpha']}/demote"

    unknown = client.post(
        "/api/team/knowledge/shared/0123456789abcdef/demote",
        json={"reason": "x"},
        headers=_operator(),
    )
    above = client.post(
        f"/api/team/knowledge/shared/{refs['bravo']}/demote",
        json={"reason": "x"},
        headers=_operator(),
    )
    blank = client.post(url, json={"reason": "  "}, headers=_operator())
    extra = client.post(url, json={"reason": "x", "force": True}, headers=_operator())

    assert unknown.status_code == 404
    assert above.status_code == 403, "an operator cannot demote what it cannot read"
    assert blank.status_code == 422
    assert extra.status_code == 422
    assert all(r["outcome"] == "denied" for r in worm.records("knowledge.demote"))
    assert len(client.get("/api/team/knowledge/shared", headers=_viewer()).json()["documents"])


def test_demote_without_operator_custody_is_503(app_with_shared) -> None:
    app, _dids, refs = app_with_shared
    worm = _with_operator(app)

    def _no_key() -> Any:
        raise RuntimeError("operator signing authority is unavailable")

    app.state.operator_signer_factory = _no_key
    resp = TestClient(app).post(
        f"/api/team/knowledge/shared/{refs['alpha']}/demote",
        json={"reason": "stale"},
        headers=_operator(),
    )

    assert resp.status_code == 503
    assert [r["outcome"] for r in worm.records("knowledge.demote")] == ["failed"]
