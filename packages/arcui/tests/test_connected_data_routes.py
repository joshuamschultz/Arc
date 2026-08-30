"""Operator journey tests for the connected-data capability boundary."""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any

from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.connected_data import routes


@dataclass(frozen=True)
class _Description:
    source_kind: str = "dropbox"
    display_name: str = "Olivia's Dropbox"


@dataclass(frozen=True)
class _State:
    pages: int = 3
    bytes_processed: int = 4096
    error_code: str | None = None
    # The successful-sync time lives on the durable sync state, not the status.
    last_synced_at: str | None = "2026-08-29T01:00:00+00:00"


@dataclass(frozen=True)
class _SourceStatus:
    connection_id: str = "dropbox-olivia"
    source_id: str = "source-7c2"
    status: str = "awaiting_mapping"
    detail: str = "operator mapping required"
    description: _Description = _Description()
    state: _State = _State()
    # None here on purpose: the wire must read the time from the durable state.
    last_synced_at: str | None = None
    documents_indexed: int = 12
    allowed_homes: tuple[str, ...] = ("document", "blob")


@dataclass(frozen=True)
class _Proposal:
    source_id: str = "source-7c2"
    homes: tuple[str, ...] = ("document", "blob")
    status: str = "pending"
    approval_id: str = "approval-map-1"
    detail: str = "awaiting operator approval"
    allowed_homes: tuple[str, ...] = ("document", "blob")


@dataclass(frozen=True)
class _Resource:
    resource_id: str = "folder:projects"
    label: str = "Projects"
    resource_kind: str = "folder"
    selected: bool = True
    detail: str = ""


@dataclass(frozen=True)
class _Provenance:
    source: str = "source-7c2"
    external_id: str = "dropbox:/projects/profile.txt"


@dataclass(frozen=True)
class _Review:
    fact_id: str = "fact-1"
    profile_id: str = "user-olivia"
    field: str = "timezone"
    value: str = "America/Chicago"
    kind: str = "inferred"
    status: str = "pending"
    classification: str = "unclassified"
    provenance: _Provenance = _Provenance()
    replaces_fact_id: str | None = None


class _Service:
    def __init__(self) -> None:
        self.staged: tuple[str, tuple[str, ...]] | None = None
        self.action: tuple[str, str] | None = None
        self.resources: tuple[str, ...] | None = None
        self.review_decision: tuple[str, str] | None = None

    async def list_sources(self) -> tuple[_SourceStatus, ...]:
        return (_SourceStatus(),)

    async def get_mapping_proposal(self, connection_id: str) -> _Proposal | None:
        return _Proposal() if connection_id == "dropbox-olivia" else None

    async def stage_mapping(self, connection_id: str, *, homes: tuple[str, ...]) -> _Proposal:
        self.staged = (connection_id, homes)
        return _Proposal(homes=homes)

    async def list_resources(self, connection_id: str) -> tuple[_Resource, ...]:
        return (_Resource(),) if connection_id == "dropbox-olivia" else ()

    async def select_resources(
        self, connection_id: str, *, resource_ids: tuple[str, ...]
    ) -> tuple[_Resource, ...]:
        self.resources = resource_ids
        return tuple(
            _Resource(resource_id=resource_id, selected=True) for resource_id in resource_ids
        )

    async def list_review_items(
        self, *, status: str | None = None, source_id: str | None = None
    ) -> tuple[_Review, ...]:
        item = _Review()
        return (
            (item,)
            if status in {None, item.status} and source_id in {None, item.provenance.source}
            else ()
        )

    async def resolve_review(self, review_id: str, decision: str) -> _Review | None:
        self.review_decision = (review_id, decision)
        return (
            _Review(status="approved" if decision == "approve" else "declined")
            if review_id == "fact-1"
            else None
        )

    async def sync_now(self, connection_id: str) -> bool:
        self.action = ("sync", connection_id)
        return True

    async def pause(self, connection_id: str) -> bool:
        self.action = ("pause", connection_id)
        return True

    async def resume(self, connection_id: str) -> bool:
        self.action = ("resume", connection_id)
        return True

    async def reindex(self, connection_id: str) -> bool:
        self.action = ("reindex", connection_id)
        return True

    async def revoke(self, connection_id: str) -> bool:
        self.action = ("revoke", connection_id)
        return True


class _Registry:
    def __init__(self, service: _Service) -> None:
        self._entry = SimpleNamespace(instance=SimpleNamespace(service=service))

    async def get_capability(self, name: str) -> Any:
        return self._entry if name == "connected_data" else None


def _client() -> tuple[TestClient, _Service]:
    service = _Service()
    app = Starlette(routes=routes)
    app.add_middleware(
        AuthMiddleware,
        auth_config=AuthConfig({"viewer_token": "viewer", "operator_token": "operator"}),
    )
    app.state.roster_provider = lambda: [SimpleNamespace(agent_id="olivia", did="did:arc:olivia")]
    app.state.embedded_agent_cache = {
        "did:arc:olivia": SimpleNamespace(_capability_registry=_Registry(service))
    }
    return TestClient(app), service


def test_activate_connected_data_is_operator_gated_and_uses_persistent_agent_seam() -> None:
    client, _service = _client()
    agent = client.app.state.embedded_agent_cache["did:arc:olivia"]
    calls: list[str] = []

    async def enable_module(name: str) -> str:
        calls.append(name)
        return "module 'connected_data' enabled"

    agent.enable_module_persisted = enable_module
    path = "/api/agents/olivia/knowledge/connected-data/activate"

    denied = client.post(path, headers={"Authorization": "Bearer viewer"})
    assert denied.status_code == 403
    assert calls == []

    activated = client.post(path, headers={"Authorization": "Bearer operator"})
    assert activated.status_code == 200
    assert activated.json() == {
        "status": "activated",
        "detail": "module 'connected_data' enabled",
    }
    assert calls == ["connected_data"]


def test_activate_connected_data_hides_runtime_failure() -> None:
    client, _service = _client()
    agent = client.app.state.embedded_agent_cache["did:arc:olivia"]

    async def enable_module(name: str) -> str:
        del name
        raise RuntimeError("missing module at /sensitive/runtime/path")

    agent.enable_module_persisted = enable_module
    response = client.post(
        "/api/agents/olivia/knowledge/connected-data/activate",
        headers={"Authorization": "Bearer operator"},
    )
    assert response.status_code == 503
    assert response.json() == {"error": "connected-data module could not be activated"}


def test_connected_sources_exposes_connected_account_before_ingest() -> None:
    client, _service = _client()
    response = client.get(
        "/api/agents/olivia/knowledge/connected-sources",
        headers={"Authorization": "Bearer viewer"},
    )
    assert response.status_code == 200
    item = response.json()["items"][0]
    assert item == {
        "connection_id": "dropbox-olivia",
        "source_id": "source-7c2",
        "label": "Olivia's Dropbox",
        "source_kind": "dropbox",
        "status": "awaiting_mapping",
        "detail": "operator mapping required",
        "pages": 3,
        "bytes_processed": 4096,
        "error_code": None,
        "last_synced_at": "2026-08-29T01:00:00+00:00",
        "documents_indexed": 12,
        "allowed_homes": ["document", "blob"],
    }


def test_mapping_is_operator_gated_and_stages_typed_homes() -> None:
    client, service = _client()
    path = "/api/agents/olivia/knowledge/connected-sources/dropbox-olivia/mapping"
    denied = client.post(
        path, headers={"Authorization": "Bearer viewer"}, json={"homes": ["document"]}
    )
    assert denied.status_code == 403

    invalid = client.post(
        path, headers={"Authorization": "Bearer operator"}, json={"homes": ["nope"]}
    )
    assert invalid.status_code == 400

    staged = client.post(
        path,
        headers={"Authorization": "Bearer operator"},
        json={"homes": ["document", "blob"]},
    )
    assert staged.status_code == 200
    assert service.staged == ("dropbox-olivia", ("document", "blob"))
    assert staged.json()["item"]["approval_id"] == "approval-map-1"


def test_reindex_is_an_audited_operator_lifecycle_action() -> None:
    client, service = _client()
    response = client.post(
        "/api/agents/olivia/knowledge/sync/dropbox-olivia/reindex",
        headers={"Authorization": "Bearer operator"},
    )
    assert response.status_code == 200
    assert response.json()["action"] == "reindex"
    assert service.action == ("reindex", "dropbox-olivia")


def test_resource_scope_is_visible_and_operator_gated() -> None:
    client, service = _client()
    path = "/api/agents/olivia/knowledge/connected-sources/dropbox-olivia/resources"
    listed = client.get(path, headers={"Authorization": "Bearer viewer"})
    assert listed.status_code == 200
    assert listed.json()["items"] == [
        {
            "resource_id": "folder:projects",
            "label": "Projects",
            "resource_kind": "folder",
            "selected": True,
            "detail": "",
        }
    ]
    denied = client.post(
        path, headers={"Authorization": "Bearer viewer"}, json={"resource_ids": []}
    )
    assert denied.status_code == 403
    selected = client.post(
        path,
        headers={"Authorization": "Bearer operator"},
        json={"resource_ids": ["folder:projects"]},
    )
    assert selected.status_code == 200
    assert service.resources == ("folder:projects",)


def test_profile_review_is_operator_only_and_exposes_provenance() -> None:
    client, service = _client()
    path = "/api/agents/olivia/knowledge/profile-reviews?status=pending&source_id=source-7c2"
    denied = client.get(path, headers={"Authorization": "Bearer viewer"})
    assert denied.status_code == 403
    listed = client.get(path, headers={"Authorization": "Bearer operator"})
    assert listed.status_code == 200
    item = listed.json()["items"][0]
    assert item["field"] == "timezone"
    assert item["source_id"] == "source-7c2"

    resolved = client.post(
        "/api/agents/olivia/knowledge/profile-reviews/fact-1/approve",
        headers={"Authorization": "Bearer operator"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "approved"
    assert service.review_decision == ("fact-1", "approve")
