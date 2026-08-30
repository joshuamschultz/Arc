"""H-026 — arcui doc-repository index route over the real ingest path.

``GET /api/agents/{agent_id}/knowledge/sources/{source_id}/index`` surfaces one
connected DOCUMENT source's verified OKF ``index.md`` (what's inside + purpose).

Every case drives a real workspace: the source is ingested through
``ConnectedDataService`` (approve mapping -> ingest), exactly as production does, so
the index.md the route reads is the one the pipeline wrote — never a hand-rolled
fixture. The abuse cases assert the four pillars: viewer 403, tampered index
fail-closed, cross-source isolation, and an ungranted source reading empty.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from arcmemory.config import MemoryConfig
from arcmemory.connected_data import (
    ConnectedDataService,
    ConnectedObject,
    ConnectedSource,
    ConnectedSourceShape,
    SourceContent,
    SourceMappingPendingError,
)
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

VIEWER_TOKEN = "viewer-doc-repo"
OPERATOR_TOKEN = "operator-doc-repo"
_DID = "did:arc:agent:doc-repo"


def _operator() -> dict[str, str]:
    return {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEWER_TOKEN}"}


def _source() -> ConnectedSource:
    return ConnectedSource(
        connection_id="dropbox",
        account_id="acct-1",
        source_kind="dropbox",
        data_shape=ConnectedSourceShape.DOCUMENT,
    )


async def _seed_index(workspace: Path) -> str:
    """Ingest one document through the real path; return the source id."""
    approval = ApprovalStore(FakeBackend())
    service = ConnectedDataService(
        workspace, _DID, approval_store=approval, config=MemoryConfig(doc_chunk_tokens=32)
    )
    source = _source()
    with pytest.raises(SourceMappingPendingError):
        await service.require_approved_mapping(source)
    pending = (await approval.list())[0]
    await approval.resolve(pending.id, status="approved", actor_did="did:operator")
    mapping = await service.require_approved_mapping(source)
    await service.ingest(
        source,
        ConnectedObject(
            object_id="q3",
            locator="/reports/q3.txt",
            version="1",
            media_type="text/plain",
            classification="unclassified",
            revision=1,
        ),
        SourceContent(
            object_id="q3", version="1", media_type="text/plain", content=b"quarterly revenue"
        ),
        mapping,
    )
    return mapping.source_id


def _make_agent_dir(team_root: Path, name: str) -> Path:
    agent_dir = team_root / name
    agent_dir.mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(f"[agent]\nname = '{name}'\n")
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
def app_with_index(tmp_path: Path) -> Iterator[tuple[Any, str]]:
    """A real app + a workspace with one ingested document source (index.md written)."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    agent_dir = _make_agent_dir(team_root, "repo-agent")
    source_id = asyncio.run(_seed_index(agent_dir / "workspace"))

    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: [_roster_entry("repo-agent", _DID, agent_dir)]
    yield app, source_id


@pytest.fixture
def app_fresh(tmp_path: Path) -> Iterator[Any]:
    """A real app for an agent that never connected a source (empty state)."""
    team_root = tmp_path / "team"
    team_root.mkdir()
    agent_dir = _make_agent_dir(team_root, "fresh")
    auth = AuthConfig({"viewer_token": VIEWER_TOKEN, "operator_token": OPERATOR_TOKEN})
    app = create_app(team_root=team_root, auth_config=auth)
    app.state.roster_provider = lambda: [
        _roster_entry("fresh", "did:arc:agent:fresh", agent_dir)
    ]
    yield app


def _url(agent_id: str, source_id: str) -> str:
    return f"/api/agents/{agent_id}/knowledge/sources/{source_id}/index"


def test_operator_reads_verified_index(app_with_index: tuple[Any, str]) -> None:
    app, source_id = app_with_index
    with TestClient(app) as client:
        resp = client.get(_url("repo-agent", source_id), headers=_operator())
    assert resp.status_code == 200
    body = resp.json()
    assert body["present"] and body["verified"]
    assert body["document_count"] == 1 and body["entries"]
    assert body["markdown"].startswith("# Collection Index")


def test_viewer_is_forbidden(app_with_index: tuple[Any, str]) -> None:
    app, source_id = app_with_index
    with TestClient(app) as client:
        resp = client.get(_url("repo-agent", source_id), headers=_viewer())
    assert resp.status_code == 403


def test_no_auth_is_401(app_with_index: tuple[Any, str]) -> None:
    app, source_id = app_with_index
    with TestClient(app) as client:
        resp = client.get(_url("repo-agent", source_id))
    assert resp.status_code == 401


def test_unknown_agent_is_404(app_with_index: tuple[Any, str]) -> None:
    app, source_id = app_with_index
    with TestClient(app) as client:
        resp = client.get(_url("ghost", source_id), headers=_operator())
    assert resp.status_code == 404


def test_tampered_index_is_fail_closed(app_with_index: tuple[Any, str]) -> None:
    app, source_id = app_with_index
    # Corrupt the on-disk index after ingest (operator hand-edit / ASI06).
    agent_dir = Path(app.state.roster_provider()[0].workspace_path)
    index_path = agent_dir / "workspace" / "memory" / "connected" / source_id / "index.md"
    index_path.write_text(index_path.read_text() + "\ntampered\n", encoding="utf-8")

    with TestClient(app) as client:
        resp = client.get(_url("repo-agent", source_id), headers=_operator())
    assert resp.status_code == 200
    body = resp.json()
    assert body["present"] and not body["verified"]
    assert body["markdown"] == "" and not body["entries"] and body["error"]
    # The wire carries an operator-actionable recovery instruction, not just a reason.
    assert body["guidance"] and "re-sync" in body["guidance"].lower()


def test_ungranted_source_reads_empty(app_with_index: tuple[Any, str]) -> None:
    app, _source_id = app_with_index
    with TestClient(app) as client:
        resp = client.get(_url("repo-agent", "some-other-source"), headers=_operator())
    assert resp.status_code == 200
    body = resp.json()
    assert not body["present"] and not body["verified"]
    assert body["markdown"] == "" and body["error"] is None


def test_fresh_agent_reads_empty(app_fresh: Any) -> None:
    with TestClient(app_fresh) as client:
        resp = client.get(_url("fresh", "anything"), headers=_operator())
    assert resp.status_code == 200
    assert resp.json()["present"] is False


def test_path_traversal_source_id_never_escapes(app_with_index: tuple[Any, str]) -> None:
    app, _source_id = app_with_index
    with TestClient(app) as client:
        # URL normalization rejects a dotted segment before routing (404); a
        # url-safe-but-unknown id is a clean empty read. Either way, nothing
        # outside the agent's connected root is ever read. (The operator's own
        # traversal guard is proven directly in the arcmemory unit suite.)
        assert client.get(_url("repo-agent", ".."), headers=_operator()).status_code == 404
        weird = client.get(_url("repo-agent", "..%2F..%2Fetc"), headers=_operator())
    assert weird.status_code in (200, 404)
    if weird.status_code == 200:
        body = weird.json()
        assert not body["present"] and not body["verified"]
