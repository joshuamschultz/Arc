"""Settings -> Maintenance -> Team members: the arcteam registry from the browser.

The dashboard reads and writes the same :class:`arcteam.registry.EntityRegistry`
``arc team register`` / ``arc team entities`` use. An operator can add a person,
and can switch a member off or remove them; nobody else can change anything, and
the operator's own entry is protected because every dashboard message is sent as
it.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcteam.audit import AuditLogger
from arcteam.registry import EntityRegistry
from arcteam.storage import MemoryBackend
from arcteam.types import Entity, EntityType
from arctrust.signer import InProcessSigner
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

OP_TOKEN = "o" * 64
VIEW_TOKEN = "v" * 64
_OPERATOR_DID = "did:arc:local:user/operator"
_AGENT_DID = "did:arc:local:agent/intake"


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def audit_event(self, name: str, fields: dict[str, Any]) -> None:
        self.events.append(dict(fields))

    def outcomes(self, operation: str) -> list[str]:
        return [e["outcome"] for e in self.events if e.get("operation") == operation]


@pytest.fixture
async def registry() -> EntityRegistry:
    backend = MemoryBackend()
    audit = AuditLogger(backend, InProcessSigner(b"\x22" * 32))
    await audit.initialize()
    registry = EntityRegistry(backend, audit)
    await registry.register(
        Entity(
            did=_OPERATOR_DID,
            handle="operator",
            id="user://operator",
            name="Operator",
            type=EntityType.USER,
        )
    )
    await registry.register(
        Entity(
            did=_AGENT_DID,
            handle="intake",
            id="agent://intake",
            name="Intake",
            type=EntityType.AGENT,
            roles=["executor"],
        )
    )
    return registry


@pytest.fixture
def audit() -> AuditRecorder:
    return AuditRecorder()


@pytest.fixture
def client(tmp_path: Path, registry: EntityRegistry, audit: AuditRecorder) -> TestClient:
    team_root = tmp_path / "team"
    team_root.mkdir()
    app = create_app(
        auth_config=AuthConfig({"viewer_token": VIEW_TOKEN, "operator_token": OP_TOKEN}),
        team_root=team_root,
    )
    app.state.audit = audit
    app.state.messaging_registry = registry
    return TestClient(app)


def _op() -> dict[str, str]:
    return {"Authorization": f"Bearer {OP_TOKEN}"}


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEW_TOKEN}"}


def _add(client: TestClient, headers: dict[str, str], **body: Any):
    payload = {"handle": "dana", "name": "Dana Reyes", **body}
    return client.post("/api/maintenance/team/members", json=payload, headers=headers)


def test_a_viewer_sees_agents_and_people(client: TestClient) -> None:
    resp = client.get("/api/maintenance/team/members", headers=_viewer())

    assert resp.status_code == 200
    by_handle = {m["handle"]: m for m in resp.json()["members"]}
    assert by_handle["intake"]["type"] == "agent"
    assert by_handle["operator"]["type"] == "user"
    assert by_handle["operator"]["protected"] is True
    assert by_handle["intake"]["protected"] is False
    assert by_handle["intake"]["status"] == "active"


def test_an_operator_adds_a_person(client: TestClient, audit: AuditRecorder) -> None:
    resp = _add(client, _op(), roles=["reviewer"])

    assert resp.status_code == 201, resp.text
    assert resp.json()["handle"] == "dana"
    members = client.get("/api/maintenance/team/members", headers=_viewer()).json()["members"]
    dana = next(m for m in members if m["handle"] == "dana")
    assert (dana["type"], dana["name"], dana["roles"], dana["status"]) == (
        "user",
        "Dana Reyes",
        ["reviewer"],
        "active",
    )
    assert dana["did"].startswith("did:")
    assert audit.outcomes("team.member.add") == ["applied"]


def test_a_viewer_cannot_add_a_member(
    client: TestClient, registry: EntityRegistry, audit: AuditRecorder
) -> None:
    resp = _add(client, _viewer())

    assert resp.status_code == 403
    members = client.get("/api/maintenance/team/members", headers=_viewer()).json()["members"]
    assert {m["handle"] for m in members} == {"operator", "intake"}
    assert audit.outcomes("team.member.add") == ["denied"]


@pytest.mark.parametrize(
    "handle", ["Dana", "d", "../x", "a b", "x" * 41, "user://dana", "dana\n", ""]
)
def test_a_handle_must_be_a_plain_lowercase_word(client: TestClient, handle: str) -> None:
    resp = _add(client, _op(), handle=handle)

    assert resp.status_code == 400
    assert "arc " not in resp.json()["error"]


def test_a_taken_handle_is_refused_in_plain_words(client: TestClient) -> None:
    assert _add(client, _op()).status_code == 201

    again = _add(client, _op())

    assert again.status_code == 409
    assert "already" in again.json()["error"]


def test_unknown_fields_and_a_foreign_type_are_refused(client: TestClient) -> None:
    """A person is always a person: the body cannot name a DID, a key or an agent type."""
    for extra in ({"did": "did:arc:x"}, {"type": "agent"}, {"public_key": "ab"}):
        assert _add(client, _op(), **extra).status_code == 400


def test_an_operator_disables_enables_and_removes_a_member(
    client: TestClient, audit: AuditRecorder
) -> None:
    did = _add(client, _op()).json()["did"]

    def _status(value: str, headers: dict[str, str] | None = None):
        return client.post(
            "/api/maintenance/team/members/status",
            json={"did": did, "status": value},
            headers=headers or _op(),
        )

    assert _status("suspended").json()["status"] == "suspended"
    assert _status("active").json()["status"] == "active"
    assert _status("revoked").json()["status"] == "revoked"
    assert audit.outcomes("team.member.status") == ["applied", "applied", "applied"]
    members = client.get("/api/maintenance/team/members", headers=_viewer()).json()["members"]
    assert next(m for m in members if m["did"] == did)["status"] == "revoked"


def test_a_viewer_cannot_change_a_members_status(
    client: TestClient, registry: EntityRegistry, audit: AuditRecorder
) -> None:
    resp = client.post(
        "/api/maintenance/team/members/status",
        json={"did": _AGENT_DID, "status": "suspended"},
        headers=_viewer(),
    )

    assert resp.status_code == 403
    assert audit.outcomes("team.member.status") == ["denied"]
    members = client.get("/api/maintenance/team/members", headers=_viewer()).json()["members"]
    assert next(m for m in members if m["did"] == _AGENT_DID)["status"] == "active"


def test_the_operators_own_entry_cannot_be_switched_off(client: TestClient) -> None:
    resp = client.post(
        "/api/maintenance/team/members/status",
        json={"did": _OPERATOR_DID, "status": "revoked"},
        headers=_op(),
    )

    assert resp.status_code == 409
    assert "operator" in resp.json()["error"].lower()


@pytest.mark.parametrize(
    "body",
    [
        {"did": _AGENT_DID, "status": "deleted"},
        {"did": _AGENT_DID},
        {"did": "intake", "status": "suspended"},
        {"did": "did:arc:nobody", "status": "suspended"},
        {"status": "suspended"},
    ],
)
def test_a_bad_status_change_is_refused(client: TestClient, body: dict[str, str]) -> None:
    resp = client.post("/api/maintenance/team/members/status", json=body, headers=_op())

    assert resp.status_code in (400, 404)


def test_with_team_messaging_offline_the_list_says_so(client: TestClient) -> None:
    client.app.state.messaging_registry = None

    resp = client.get("/api/maintenance/team/members", headers=_viewer())

    assert resp.status_code == 503
    assert "messaging" in resp.json()["error"].lower()
