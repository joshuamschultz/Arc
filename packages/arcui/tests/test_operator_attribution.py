"""SPEC-087 T-1284: operator routes record WHO acted, never a fixed operator DID.

REQ-569: approving or denying an approval, revoking a standing grant, requesting
a run cancellation, acknowledging an approval notification and merging (or
refusing) entity duplicates from arcui record the acting identity as the actor.
Approval grants stay signed by the deployment operator authority.

REQ-571: an approval decision on the static operator token (no named person) is
refused 403 ``operator_account_required`` with one audit row at federal, and at
enterprise when ``require_named_decider`` is true; enterprise by default allows
it with one ``operator_token_decision`` warning row; personal allows it.

These routes are not app-reachable (the middleware refuses principals there),
so the callers here are people's sessions and the static operator token.

Real ``create_app`` + real ``AuthMiddleware`` + real ``UserStore`` (via
``principal_fixtures``). The approval/standing-grant/cancel stores and the
notification hub are the production classes over arcstore's in-memory
``FakeBackend``; only the backend is a double, so the attribution each store
persists is read back exactly as the route wrote it.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcgateway.team_roster import RosterEntry
from arcmemory.db import MemoryDB
from arcmemory.index.graph import WeightedGraph
from arcmemory.stores.semantic import SemanticStore
from arcstore.approval_dispatcher import ApprovalNotification
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arcstore.cancellations import CancelStore
from arcstore.standing_grants import StandingGrantStore
from arctrust import OperatorKey, default_operator_key_path
from arctrust.audit import AuditEvent
from arctrust.policy import OperatorApprovalAuthority, grant_from_wire
from arctrust.users import OPERATOR
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    PrincipalApp,
    build_principal_app,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

from arcui.approval_notifications import ApprovalNotificationHub

FIXED_OPERATOR_DID = "did:arc:ui:operator"
SESSION_PSEUDO_DID = re.compile(r"^did:arc:ui:session:[0-9a-f]+$")
OPERATOR_ACCOUNT_REQUIRED = "operator_account_required"
UNNAMED_DECISION = "operator_token_decision"
AGENT = "did:arc:test:exec/agent1"
APPROVAL_ID = "req1"
EVENT_ID = "event-1"
SRC = Path(__file__).resolve().parents[1] / "src" / "arcui"
ATTRIBUTION_FILES = (
    SRC / "routes" / "approvals.py",
    SRC / "routes" / "standing_grants.py",
    SRC / "routes" / "cancellations.py",
    SRC / "approval_notifications.py",
    SRC / "routes" / "knowledge.py",
)
STATIC = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}


# --- storage-boundary doubles ------------------------------------------------------


@dataclass
class _StoreSink:
    """The arcstore audit sink: every row a store writes, with its actor."""

    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class _RecordingBackend(FakeBackend):
    """FakeBackend that remembers the actor of every conditional update.

    ``ApprovalNotificationHub.acknowledge`` persists its ack through
    ``update_if`` without an audit sink, so the actor it passes is only visible
    at the backend boundary.
    """

    def __init__(self) -> None:
        super().__init__()
        self.update_actors: list[tuple[str, str]] = []

    async def update_if(self, collection: str, key: str, *args: Any, **kwargs: Any) -> Any:
        self.update_actors.append((collection, str(kwargs.get("actor_did"))))
        return await super().update_if(collection, key, *args, **kwargs)


# --- the deployment under test ------------------------------------------------------


@dataclass
class _Deployment:
    built: PrincipalApp
    backend: _RecordingBackend
    sink: _StoreSink
    approvals: ApprovalStore
    standing: StandingGrantStore
    cancels: CancelStore
    hub: ApprovalNotificationHub
    operator_did: str

    @property
    def client(self) -> TestClient:
        return TestClient(self.built.app, raise_server_exceptions=False)

    def person(self, email: str = "ada@example.com") -> tuple[dict[str, str], str]:
        token, did = self.built.sign_in(email, (OPERATOR,))
        return {"Authorization": f"Bearer {token}"}, did

    def approval(self) -> PendingApproval:
        row = asyncio.run(self.approvals.get(APPROVAL_ID))
        assert row is not None
        return row

    def worm_rows(self) -> list[Any]:
        return list(self.built.worm.written)


def _seed(approvals: ApprovalStore, hub: ApprovalNotificationHub) -> None:
    async def _run() -> None:
        await approvals.create(
            PendingApproval(
                id=APPROVAL_ID,
                agent_did=AGENT,
                agent_label="olivia",
                tool="dropbox_upload",
                legs=["external_comms", "private_data", "untrusted_input"],
                call_hash="abc",
                destination="personal_dropbox",
                grant_tool="dropbox_upload",
                standing_eligible=True,
            )
        )
        await hub(
            ApprovalNotification(
                event_id=EVENT_ID,
                approval_id=APPROVAL_ID,
                status="pending",
                agent_did=AGENT,
                tool="dropbox_upload",
                classification="UNCLASSIFIED",
                attempts=1,
            )
        )

    asyncio.run(_run())


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=True)
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def deploy(tmp_path: Path, users: Any) -> Callable[..., _Deployment]:
    def _make(tier: str = "personal", **config: Any) -> _Deployment:
        built = build_principal_app(
            tmp_path / tier,
            principal_store_factory=None,
            users=users,
            tier=tier,
            **config,
        )
        backend = _RecordingBackend()
        asyncio.run(backend.start())
        sink = _StoreSink()
        approvals = ApprovalStore(backend, sink=sink)
        standing = StandingGrantStore(backend, sink=sink)
        cancels = CancelStore(backend, sink=sink)
        hub = ApprovalNotificationHub(backend=backend)
        state = built.app.state
        state.approval_store = approvals
        state.standing_grant_store = standing
        state.cancel_store = cancels
        state.approval_notification_hub = hub
        _seed(approvals, hub)
        key = OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=False)
        operator_did = OperatorApprovalAuthority(key.into_signer()).did
        return _Deployment(built, backend, sink, approvals, standing, cancels, hub, operator_did)

    return _make


def _store_actors(deployment: _Deployment, action: str) -> list[str]:
    return [e.actor_did for e in deployment.sink.events if e.action == action]


# --- REQ-569: approvals ------------------------------------------------------------


class TestApprovalAttribution:
    def test_deny_records_the_signed_in_person(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, person = deployment.person()

        resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/deny", headers=headers)

        assert resp.status_code == 200, resp.text
        row = deployment.approval()
        assert row.status == "denied"
        assert row.resolved_by == person
        assert _store_actors(deployment, "approval.resolve") == [person]

    def test_approve_keeps_the_operator_signed_grant_and_records_the_person(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, person = deployment.person()

        resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/approve", headers=headers)

        assert resp.status_code == 200, resp.text
        row = deployment.approval()
        assert row.status == "approved"
        assert row.grant is not None
        assert grant_from_wire(row.grant).approver_did == deployment.operator_did
        assert row.resolved_by == person

    def test_always_allow_keeps_the_operator_grant_and_records_the_person(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, person = deployment.person()

        resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/always", headers=headers)

        assert resp.status_code == 200, resp.text
        row = deployment.approval()
        assert row.status == "approved"
        assert row.grant is not None
        assert grant_from_wire(row.grant).approver_did == deployment.operator_did
        assert row.resolved_by == person

    def test_static_token_deny_records_the_session_pseudo_did(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()

        resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/deny", headers=STATIC)

        assert resp.status_code == 200, resp.text
        resolved_by = deployment.approval().resolved_by
        assert resolved_by is not None
        assert SESSION_PSEUDO_DID.match(resolved_by), resolved_by


# --- REQ-571: who may decide on the static operator token --------------------------


def _refused_once(deployment: _Deployment, resp: Any) -> None:
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"] == OPERATOR_ACCOUNT_REQUIRED
    assert deployment.approval().status == "pending"
    rows = deployment.worm_rows()
    assert len(rows) == 1, rows
    assert rows[0].outcome == "denied"
    assert OPERATOR_ACCOUNT_REQUIRED in rows[0].detail


@pytest.mark.parametrize("decision", ["approve", "deny"])
class TestStaticTokenDecisions:
    def test_federal_refuses_with_one_audit_row(
        self, deploy: Callable[..., _Deployment], decision: str
    ) -> None:
        deployment = deploy("federal")

        resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/{decision}", headers=STATIC)

        _refused_once(deployment, resp)

    def test_enterprise_requiring_a_named_decider_refuses(
        self, deploy: Callable[..., _Deployment], decision: str
    ) -> None:
        deployment = deploy("enterprise", require_named_decider=True)

        resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/{decision}", headers=STATIC)

        _refused_once(deployment, resp)

    def test_enterprise_default_allows_with_one_warning_row(
        self, deploy: Callable[..., _Deployment], decision: str
    ) -> None:
        deployment = deploy("enterprise")

        resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/{decision}", headers=STATIC)

        assert resp.status_code == 200, resp.text
        row = deployment.approval()
        assert row.status == ("approved" if decision == "approve" else "denied")
        assert row.resolved_by is not None
        assert SESSION_PSEUDO_DID.match(row.resolved_by), row.resolved_by
        warnings = [r for r in deployment.worm_rows() if r.operation == UNNAMED_DECISION]
        assert len(warnings) == 1, deployment.worm_rows()

    def test_personal_allows_without_a_warning(
        self, deploy: Callable[..., _Deployment], decision: str
    ) -> None:
        deployment = deploy("personal")

        resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/{decision}", headers=STATIC)

        assert resp.status_code == 200, resp.text
        assert deployment.approval().status == ("approved" if decision == "approve" else "denied")
        assert not [r for r in deployment.worm_rows() if r.operation == UNNAMED_DECISION]

    def test_federal_still_lets_a_named_person_decide(
        self, deploy: Callable[..., _Deployment], decision: str
    ) -> None:
        deployment = deploy("federal")
        headers, person = deployment.person()

        resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/{decision}", headers=headers)

        assert resp.status_code == 200, resp.text
        assert deployment.approval().resolved_by == person


# --- REQ-569: standing grants, cancellations, notifications ------------------------


def _always_allow(deployment: _Deployment) -> str:
    resp = deployment.client.post(f"/api/approvals/{APPROVAL_ID}/always", headers=STATIC)
    assert resp.status_code == 200, resp.text
    return str(resp.json()["standing_grant"]["id"])


class TestStandingGrantRevoke:
    def test_revoke_records_the_signed_in_person(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        grant_id = _always_allow(deployment)
        headers, person = deployment.person()

        resp = deployment.client.post(f"/api/standing-grants/{grant_id}/revoke", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["revoked_by"] == person
        [stored] = asyncio.run(deployment.standing.list(status="revoked"))
        assert stored.revoked_by == person

    def test_static_token_revoke_records_the_session_pseudo_did(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        grant_id = _always_allow(deployment)

        resp = deployment.client.post(f"/api/standing-grants/{grant_id}/revoke", headers=STATIC)

        assert resp.status_code == 200, resp.text
        revoked_by = resp.json()["revoked_by"]
        assert SESSION_PSEUDO_DID.match(revoked_by), revoked_by


class TestCancellationRequest:
    def test_cancel_records_the_signed_in_person(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, person = deployment.person()

        resp = deployment.client.post(
            "/api/cancellations", headers=headers, json={"run_id": "run-abc"}
        )

        assert resp.status_code == 201, resp.text
        assert resp.json()["requested_by"] == person
        [stored] = asyncio.run(deployment.cancels.list(status="pending"))
        assert stored.requested_by == person

    def test_static_token_cancel_records_the_session_pseudo_did(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()

        resp = deployment.client.post(
            "/api/cancellations", headers=STATIC, json={"run_id": "run-abc"}
        )

        assert resp.status_code == 201, resp.text
        requested_by = resp.json()["requested_by"]
        assert SESSION_PSEUDO_DID.match(requested_by), requested_by


class TestNotificationAck:
    def _ack_actors(self, deployment: _Deployment) -> list[str]:
        return [
            actor
            for collection, actor in deployment.backend.update_actors
            if collection == "approval_notification_browser"
        ]

    def test_ack_records_the_signed_in_person(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, person = deployment.person()

        resp = deployment.client.post(
            f"/api/approvals/notifications/{EVENT_ID}/ack", headers=headers
        )

        assert resp.status_code == 200, resp.text
        assert self._ack_actors(deployment) == [person]

    def test_static_token_ack_records_the_session_pseudo_did(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()

        resp = deployment.client.post(
            f"/api/approvals/notifications/{EVENT_ID}/ack", headers=STATIC
        )

        assert resp.status_code == 200, resp.text
        [actor] = self._ack_actors(deployment)
        assert SESSION_PSEUDO_DID.match(actor), actor


# --- REQ-569: knowledge review + memory edits ---------------------------------------

KNOWLEDGE_AGENT_DID = "did:arc:agent:olivia"
ENTITIES = "/api/agents/olivia/knowledge/entities/duplicates"
MEMORIES = "/api/agents/olivia/knowledge/memories"


def _roster_entry(agent_dir: Path, did: str) -> RosterEntry:
    return RosterEntry(
        agent_id="olivia",
        name="olivia",
        did=did,
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


def _knowledge_workspace(root: Path) -> Path:
    agent_dir = root / "olivia"
    workspace = agent_dir / "workspace"
    workspace.mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        "[agent]\nname = 'olivia'\n\n[modules.memory]\nembed_backend = 'none'\n"
    )
    store = SemanticStore(workspace, WeightedGraph(MemoryDB(workspace)), scope=KNOWLEDGE_AGENT_DID)
    store.write_fact("brad", "role", "cto", name="Brad Baker", entity_type="person")
    store.write_fact("brad-b", "city", "austin", name="Brad Baker", entity_type="person")
    return agent_dir


def _with_agent(deployment: _Deployment, agent_dir: Path, did: str) -> None:
    deployment.built.app.state.roster_provider = lambda: [_roster_entry(agent_dir, did)]


def _distinct_by(agent_dir: Path) -> list[str]:
    path = agent_dir / "workspace" / "memory" / "entity-distinct.json"
    return [entry["by"] for entry in json.loads(path.read_text(encoding="utf-8"))["pairs"]]


class TestKnowledgeAttribution:
    def test_not_the_same_records_the_signed_in_person(
        self, deploy: Callable[..., _Deployment], tmp_path: Path
    ) -> None:
        # merge and reject share one attribution line in the route (_review_action);
        # reject is the one whose actor arcmemory persists, so it is the witness.
        deployment = deploy()
        agent_dir = _knowledge_workspace(tmp_path / "team")
        _with_agent(deployment, agent_dir, KNOWLEDGE_AGENT_DID)
        headers, person = deployment.person()

        resp = deployment.client.post(
            f"{ENTITIES}/reject", headers=headers, json={"slugs": ["brad", "brad-b"]}
        )

        assert resp.status_code == 200, resp.text
        assert _distinct_by(agent_dir) == [person]

    def test_static_token_not_the_same_records_the_session_pseudo_did(
        self, deploy: Callable[..., _Deployment], tmp_path: Path
    ) -> None:
        deployment = deploy()
        agent_dir = _knowledge_workspace(tmp_path / "team")
        _with_agent(deployment, agent_dir, KNOWLEDGE_AGENT_DID)

        resp = deployment.client.post(
            f"{ENTITIES}/reject", headers=STATIC, json={"slugs": ["brad", "brad-b"]}
        )

        assert resp.status_code == 200, resp.text
        [by] = _distinct_by(agent_dir)
        assert SESSION_PSEUDO_DID.match(by), by

    @pytest.mark.parametrize("method", ["PATCH", "DELETE"])
    def test_memory_edit_records_the_acting_person_never_a_fixed_operator(
        self,
        deploy: Callable[..., _Deployment],
        tmp_path: Path,
        method: str,
    ) -> None:
        # The edit result carries the actor the route passed to arcmemory; an
        # unknown entry still reports it (404 with the sub-result verbatim). The
        # person acted, not the agent whose memory it is and not a fixed operator.
        deployment = deploy()
        agent_dir = _knowledge_workspace(tmp_path / "team")
        _with_agent(deployment, agent_dir, KNOWLEDGE_AGENT_DID)
        headers, person = deployment.person()

        resp = deployment.client.request(
            method,
            f"{MEMORIES}/no-such-entry",
            headers=headers,
            json={"text": "edited"} if method == "PATCH" else None,
        )

        actors = [r["actor_did"] for r in resp.json()["results"]]
        assert actors == [person], resp.text


# --- the constant itself is gone -----------------------------------------------------


@pytest.mark.parametrize("path", ATTRIBUTION_FILES, ids=lambda p: p.name)
def test_no_fixed_operator_did_literal_remains(path: Path) -> None:
    assert FIXED_OPERATOR_DID not in path.read_text(encoding="utf-8")
