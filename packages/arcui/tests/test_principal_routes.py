"""SPEC-087 T-1286: principal issue / list / revoke routes and the approval special case.

COMP-010 routes (``arcui.routes.principals``, mounted by ``create_app``):

- ``POST /api/principals`` body ``{name, public_key (hex), scopes: {scope: [agent_did]},
  subjects: [user_did], expires_at}`` -> 201
  ``{"principal": <redacted row>, "secret": "arcp_<id>.<secret>", "status": ...}``
  plus ``"approval_id"`` when the tier keeps the principal pending;
- ``GET /api/principals`` -> ``{"principals": [<redacted row>, ...]}``;
- ``POST /api/principals/{id}/revoke`` -> 200 ``{"principal": <redacted row>}``.

The approvals approve/deny routes gain ``PRINCIPAL_ISSUE_TOOL`` (``principal_issue``)
beside ``WORKFLOW_SIGN_TOOL``: approve re-reads the record, refuses a content-hash
mismatch (409), refuses approver == requester at federal (403), then activates;
deny revokes the pending record.

Requirements: REQ-549 (DID from the app key, secret once, digest only), REQ-550
(closed scope catalogue, 400 + audit), REQ-551 (no wildcards above personal),
REQ-552 (personal: active at once), REQ-553 (enterprise/federal: pending + approval
row bound to the content hash; activate only on a matching hash), REQ-554 (federal
two-person rule), REQ-573 (operator issues/lists/revokes; never shows a secret),
REQ-574 (enterprise <= 90 d, federal <= 30 d). D-823.

Contract choices pinned here (orchestrator brief, SDD COMP-010 / Data Model):

- only a SIGNED-IN operator session may issue (static operator token, viewer
  session, viewer token and app principals are refused 403);
- validation refusals answer 400 with the DEV-2 refusal code
  (``unknown_scope``, ``wildcard_agent``, ``wildcard_subject``, ``missing_expiry``,
  ``expiry_too_long``) and write nothing to the authority or the approval store;
- the bearer secret appears in exactly one response, the issue response;
- every change attempt (applied or refused) writes one ``ui.mutation`` row whose
  ``operation`` starts with ``principal.`` (approval-route decisions keep their
  ``approval.*`` operation).

Everything runs through the REAL ``create_app`` + ``AuthMiddleware`` with a real
``UserStore``, a real anchored ``PrincipalStore`` (``principal_fixtures``) and the
production ``ApprovalStore`` over arcstore's in-memory ``FakeBackend``. "Next
principal request" checks go through the middleware to a probe mounted on the
app-reachable inbox row, so a 401 there is the middleware refusing the token.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arctrust import OperatorKey, default_operator_key_path
from arctrust.audit import AuditEvent
from arctrust.canonical import canonical_json
from arctrust.identity import did_matches_pubkey, parse_did
from arctrust.keypair import KeyPair, generate_keypair
from arctrust.principal_auth import parse_bearer, secret_digest
from arctrust.principals import Principal, Scope
from arctrust.sealed_state import SealedAuthorityState
from arctrust.users import OPERATOR, VIEWER
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    STORE_ACTOR,
    VIEWER_TOKEN,
    IssuedPrincipal,
    PrincipalApp,
    PrincipalRig,
    agent_did,
    all_scopes,
    build_principal_app,
    issue_principal,
    principal_rig,
    signed_headers,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

PRINCIPALS = "/api/principals"
PRINCIPAL_ISSUE_TOOL = "principal_issue"
OLIVIA = agent_did()
BOB = agent_did()
INBOX_PATTERN = "/api/agents/{id}/inbox"
INBOX_OLIVIA = "/api/agents/olivia/inbox"
BEARER_RE = re.compile(r"^arcp_[0-9a-f]{32}\.[A-Za-z0-9_-]{43}$")
STATIC_OPERATOR = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
STATIC_VIEWER = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
LIST_ROW_FIELDS = {
    "id",
    "name",
    "did",
    "scopes",
    "agents",
    "subjects",
    "status",
    "created_by",
    "approved_by",
    "last_used_at",
    "expires_at",
}


# --- storage-boundary double ---------------------------------------------------------


@dataclass
class _StoreSink:
    events: list[AuditEvent]

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


# --- the deployment under test -------------------------------------------------------


@dataclass
class _Deployment:
    built: PrincipalApp
    rig: PrincipalRig
    approvals: ApprovalStore
    client: TestClient

    def operator(self, email: str = "ada@example.com") -> tuple[dict[str, str], str]:
        token, did = self.built.sign_in(email, (OPERATOR,))
        return {"Authorization": f"Bearer {token}"}, did

    def viewer(self, email: str = "vic@example.com") -> tuple[dict[str, str], str]:
        token, did = self.built.sign_in(email, (VIEWER,))
        return {"Authorization": f"Bearer {token}"}, did

    def stored(self, principal_id: str) -> Principal:
        record = self.rig.open().get(principal_id)
        assert record is not None, principal_id
        return record

    def approval_rows(self) -> list[PendingApproval]:
        return asyncio.run(self.approvals.list())

    def approval(self, approval_id: str) -> PendingApproval:
        row = asyncio.run(self.approvals.get(approval_id))
        assert row is not None, approval_id
        return row

    def rows(self, prefix: str = "principal.") -> list[Any]:
        return [r for r in self.built.worm.written if r.operation.startswith(prefix)]

    def probe_status(self, headers: dict[str, str]) -> int:
        """A principal request through the real middleware to an app-reachable row."""
        return self.client.post(INBOX_OLIVIA, json={}, headers=headers).status_code


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    # The approvals approve route signs its grant with the on-box operator key.
    OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=True)
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def deploy(tmp_path: Path, users: Any) -> Callable[..., _Deployment]:
    def _make(tier: str = "personal", **config: Any) -> _Deployment:
        root = tmp_path / tier
        rig = principal_rig(root / "authority")
        team = write_team(root / "team", {"olivia": OLIVIA, "bob": BOB})
        built = build_principal_app(
            root,
            principal_store_factory=rig.factory,
            users=users,
            tier=tier,
            team_root=team,
            **config,
        )
        built.add_probe(INBOX_PATTERN)
        backend = FakeBackend()
        asyncio.run(backend.start())
        approvals = ApprovalStore(backend, sink=_StoreSink(events=[]))
        built.app.state.approval_store = approvals
        client = TestClient(built.app, raise_server_exceptions=False)
        return _Deployment(built=built, rig=rig, approvals=approvals, client=client)

    return _make


# --- request helpers -----------------------------------------------------------------


def _issue_body(
    keys: KeyPair,
    *,
    name: str = "billing-app",
    scopes: dict[str, list[str]] | None = None,
    subjects: list[str] | None = None,
    expires_in: timedelta | None = timedelta(days=7),
) -> dict[str, Any]:
    return {
        "name": name,
        "public_key": keys.public_key.hex(),
        "scopes": scopes if scopes is not None else {str(Scope.INBOX_SEND): [OLIVIA]},
        "subjects": subjects if subjects is not None else [],
        "expires_at": None if expires_in is None else (datetime.now(UTC) + expires_in).isoformat(),
    }


@dataclass(frozen=True)
class _Issued:
    keys: KeyPair
    response: dict[str, Any]

    @property
    def principal_id(self) -> str:
        return str(self.response["principal"]["id"])

    @property
    def secret(self) -> str:
        return str(self.response["secret"])

    @property
    def approval_id(self) -> str:
        return str(self.response["approval_id"])

    def bearer(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.secret}"}


def _issue(deployment: _Deployment, headers: dict[str, str], **body_kw: Any) -> _Issued:
    keys = generate_keypair()
    resp = deployment.client.post(PRINCIPALS, json=_issue_body(keys, **body_kw), headers=headers)
    assert resp.status_code == 201, resp.text
    return _Issued(keys=keys, response=resp.json())


def _signed_probe(deployment: _Deployment, issued: _Issued) -> int:
    """Federal: the next principal request is a signed one."""
    app = IssuedPrincipal(
        record=deployment.stored(issued.principal_id), bearer=issued.secret, keys=issued.keys
    )
    headers = signed_headers(app, method="POST", path=INBOX_OLIVIA, body=b"{}")
    headers["Content-Type"] = "application/json"
    return deployment.client.post(INBOX_OLIVIA, content=b"{}", headers=headers).status_code


def _tamper_stored_scopes(rig: PrincipalRig, principal_id: str) -> None:
    """A privileged writer edits the pending record (adds a scope) after the request.

    Goes through the real sealed, anchored state (COMP-002 public API), so the
    store reads the edit back as authentic: only the content hash can catch it.
    """
    state = SealedAuthorityState(
        rig.path,
        anchor=rig.anchor,
        cipher=rig.cipher,
        audit_sink=rig.audit,
        strict_audit_sink=rig.strict,
        record_name="principals",
        actor_did=STORE_ACTOR,
    )
    payload = state.load()
    assert payload is not None
    (record,) = [p for p in payload["principals"] if p["id"] == principal_id]
    record["scopes"][str(Scope.GATES_RESOLVE)] = [OLIVIA]
    state.save(payload)


# --- fixture smoke: proves the harness, not the feature ------------------------------


class TestFixtureSmoke:
    """These pass today; they prove a RED below is the missing feature, not the rig."""

    def test_operator_session_reaches_the_inbox_probe(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        assert deployment.probe_status(headers) == 200

    def test_store_issued_principal_reaches_the_probe_and_revoke_shuts_it(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        app = issue_principal(deployment.rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        assert deployment.probe_status(app.bearer_headers()) == 200
        deployment.rig.open().revoke(app.record.id, STORE_ACTOR)
        assert deployment.probe_status(app.bearer_headers()) == 401

    def test_approval_store_round_trips_a_principal_issue_row(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        asyncio.run(
            deployment.approvals.create(
                PendingApproval(
                    id="smoke",
                    agent_did=OLIVIA,
                    tool=PRINCIPAL_ISSUE_TOOL,
                    call_hash="h",
                    arguments={"principal_id": "p", "name": "n", "scopes": "{}"},
                )
            )
        )
        assert [r.id for r in deployment.approval_rows()] == ["smoke"]

    def test_sealed_state_tamper_helper_reads_back_through_the_store(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        store = deployment.rig.open()
        app = issue_principal(store, scopes={Scope.INBOX_SEND: (OLIVIA,)})
        before = deployment.stored(app.record.id).content_hash()
        _tamper_stored_scopes(deployment.rig, app.record.id)
        after = deployment.stored(app.record.id)
        assert Scope.GATES_RESOLVE in after.scopes
        assert after.content_hash() != before


# --- personal: active at once, secret once (REQ-549, REQ-552, REQ-573) ---------------


class TestPersonalIssue:
    def test_issue_returns_active_principal_and_a_working_secret_once(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, operator = deployment.operator()

        issued = _issue(deployment, headers)

        body = issued.response
        assert body["status"] == "active"
        assert body["principal"]["status"] == "active"
        assert BEARER_RE.match(issued.secret), issued.secret
        bearer_id, raw_secret = parse_bearer(issued.secret)
        assert bearer_id == issued.principal_id
        record = deployment.stored(issued.principal_id)
        assert record.status == "active"
        assert record.secret_digest == secret_digest(raw_secret)
        assert record.created_by == operator
        assert record.public_key == issued.keys.public_key.hex()
        assert deployment.approval_rows() == []
        assert deployment.probe_status(issued.bearer()) == 200

    def test_did_is_an_app_did_derived_from_the_supplied_key(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()

        issued = _issue(deployment, headers)

        did = issued.response["principal"]["did"]
        assert did_matches_pubkey(did, issued.keys.public_key)
        assert parse_did(did)["agent_type"] == "app"
        assert deployment.stored(issued.principal_id).did == did

    def test_issue_response_never_carries_the_digest(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()

        issued = _issue(deployment, headers)

        digest = deployment.stored(issued.principal_id).secret_digest
        assert "secret_digest" not in issued.response["principal"]
        assert digest not in json.dumps(issued.response)

    def test_list_shows_the_row_fields_and_never_the_secret_or_digest(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, operator = deployment.operator()
        issued = _issue(
            deployment,
            headers,
            scopes={str(Scope.INBOX_SEND): [OLIVIA], str(Scope.RUNS_READ): [BOB]},
        )

        resp = deployment.client.get(PRINCIPALS, headers=headers)

        assert resp.status_code == 200, resp.text
        (row,) = resp.json()["principals"]
        assert LIST_ROW_FIELDS <= set(row), set(row)
        assert row["id"] == issued.principal_id
        assert row["status"] == "active"
        assert row["created_by"] == operator
        assert sorted(row["agents"]) == sorted([OLIVIA, BOB])
        assert row["scopes"] == {str(Scope.INBOX_SEND): [OLIVIA], str(Scope.RUNS_READ): [BOB]}
        text = resp.text
        _, raw_secret = parse_bearer(issued.secret)
        assert raw_secret not in text
        assert issued.secret not in text
        assert deployment.stored(issued.principal_id).secret_digest not in text
        assert "secret" not in row and "secret_digest" not in row

    def test_issue_writes_one_applied_audit_row_naming_the_operator(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, operator = deployment.operator()

        issued = _issue(deployment, headers)

        applied = [r for r in deployment.rows() if r.outcome == "applied"]
        assert len(applied) == 1, deployment.built.worm.written
        assert issued.principal_id in applied[0].target
        assert applied[0].actor_did == operator
        assert issued.secret not in applied[0].detail


# --- validation (REQ-550, REQ-551, REQ-574) -------------------------------------------


class TestValidation:
    def _refused(self, deployment: _Deployment, code: str, **body_kw: Any) -> None:
        headers, _ = deployment.operator()
        resp = deployment.client.post(
            PRINCIPALS, json=_issue_body(generate_keypair(), **body_kw), headers=headers
        )
        assert resp.status_code == 400, resp.text
        assert resp.json() == {"error": code}
        assert deployment.rig.open().list() == []
        assert deployment.approval_rows() == []
        denied = [r for r in deployment.rows() if r.outcome == "denied"]
        assert len(denied) == 1, deployment.built.worm.written

    def test_unknown_scope_is_400_and_audited(self, deploy: Callable[..., _Deployment]) -> None:
        self._refused(deploy(), "unknown_scope", scopes={"admin:everything": [OLIVIA]})

    def test_enterprise_expiry_over_90_days_is_400(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        self._refused(deploy("enterprise"), "expiry_too_long", expires_in=timedelta(days=91))

    def test_federal_expiry_over_30_days_is_400(self, deploy: Callable[..., _Deployment]) -> None:
        self._refused(deploy("federal"), "expiry_too_long", expires_in=timedelta(days=31))

    def test_enterprise_missing_expiry_is_400(self, deploy: Callable[..., _Deployment]) -> None:
        self._refused(deploy("enterprise"), "missing_expiry", expires_in=None)

    def test_wildcard_agent_at_enterprise_is_400(self, deploy: Callable[..., _Deployment]) -> None:
        self._refused(
            deploy("enterprise"), "wildcard_agent", scopes={str(Scope.INBOX_SEND): ["*"]}
        )

    def test_wildcard_subject_at_federal_is_400(self, deploy: Callable[..., _Deployment]) -> None:
        self._refused(deploy("federal"), "wildcard_subject", subjects=["*"])

    def test_enterprise_expiry_just_inside_90_days_is_accepted(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        headers, _ = deployment.operator()
        issued = _issue(deployment, headers, expires_in=timedelta(days=90) - timedelta(minutes=5))
        assert issued.response["status"] == "pending"

    def test_federal_expiry_just_inside_30_days_is_accepted(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("federal")
        headers, _ = deployment.operator()
        issued = _issue(deployment, headers, expires_in=timedelta(days=30) - timedelta(minutes=5))
        assert issued.response["status"] == "pending"

    @pytest.mark.parametrize(
        "mutate",
        [
            pytest.param(lambda b: b.update(public_key="zz" * 32), id="non-hex-key"),
            pytest.param(lambda b: b.update(public_key="ab" * 16), id="short-key"),
            pytest.param(lambda b: b.update(name="x" * 65), id="name-too-long"),
            pytest.param(lambda b: b.update(name=""), id="empty-name"),
            pytest.param(lambda b: b.update(extra="field"), id="extra-field"),
            pytest.param(lambda b: b.pop("public_key"), id="missing-key"),
            pytest.param(lambda b: b.update(scopes={}), id="no-scopes"),
            pytest.param(
                lambda b: b.update(scopes={str(Scope.INBOX_SEND): []}), id="scope-without-agent"
            ),
        ],
    )
    def test_malformed_body_is_400_and_writes_nothing(
        self, deploy: Callable[..., _Deployment], mutate: Callable[[dict[str, Any]], Any]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        body = _issue_body(generate_keypair())
        mutate(body)

        resp = deployment.client.post(PRINCIPALS, json=body, headers=headers)

        assert resp.status_code == 400, resp.text
        assert deployment.rig.open().list() == []
        assert [r.outcome for r in deployment.rows()] == ["denied"]

    def test_malformed_json_is_400(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        resp = deployment.client.post(
            PRINCIPALS,
            content=b"{not json",
            headers={**headers, "Content-Type": "application/json"},
        )
        assert resp.status_code == 400, resp.text
        assert deployment.rig.open().list() == []

    def test_same_key_twice_is_refused_and_keeps_one_record(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        keys = generate_keypair()
        first = deployment.client.post(PRINCIPALS, json=_issue_body(keys), headers=headers)
        assert first.status_code == 201, first.text

        second = deployment.client.post(PRINCIPALS, json=_issue_body(keys), headers=headers)

        assert second.status_code in {400, 409}, second.text
        assert len(deployment.rig.open().list()) == 1
        assert "secret" not in second.json()


# --- callers who may not issue (COMP-010: signed-in operator only) -------------------


class TestCallerRefusals:
    def test_viewer_session_issue_is_403_and_audited(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.viewer()

        resp = deployment.client.post(
            PRINCIPALS, json=_issue_body(generate_keypair()), headers=headers
        )

        assert resp.status_code == 403, resp.text
        assert deployment.rig.open().list() == []
        assert [r.outcome for r in deployment.rows()] == ["denied"]

    def test_viewer_session_cannot_list(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, _ = deployment.viewer()
        resp = deployment.client.get(PRINCIPALS, headers=headers)
        assert resp.status_code == 403, resp.text

    def test_static_operator_token_issue_is_403_and_audited(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()

        resp = deployment.client.post(
            PRINCIPALS, json=_issue_body(generate_keypair()), headers=STATIC_OPERATOR
        )

        assert resp.status_code == 403, resp.text
        assert deployment.rig.open().list() == []
        assert [r.outcome for r in deployment.rows()] == ["denied"]

    def test_static_viewer_token_issue_is_403(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        resp = deployment.client.post(
            PRINCIPALS, json=_issue_body(generate_keypair()), headers=STATIC_VIEWER
        )
        assert resp.status_code == 403, resp.text
        assert deployment.rig.open().list() == []

    def test_viewer_session_revoke_is_403_and_keeps_the_principal_active(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        op_headers, _ = deployment.operator()
        issued = _issue(deployment, op_headers)
        viewer_headers, _ = deployment.viewer()

        resp = deployment.client.post(
            f"{PRINCIPALS}/{issued.principal_id}/revoke", headers=viewer_headers
        )

        assert resp.status_code == 403, resp.text
        assert deployment.stored(issued.principal_id).status == "active"
        assert deployment.probe_status(issued.bearer()) == 200

    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_principal_caller_is_hard_denied(
        self, deploy: Callable[..., _Deployment], method: str
    ) -> None:
        # Middleware-level refusal (hard-denied prefix): green before T-1286 by
        # design; kept as a regression guard for the new routes.
        deployment = deploy()
        app = issue_principal(deployment.rig.open(), scopes=all_scopes())
        resp = deployment.client.request(
            method,
            PRINCIPALS,
            json=_issue_body(generate_keypair()) if method == "POST" else None,
            headers=app.bearer_headers(),
        )
        assert resp.status_code == 403, resp.text
        assert len(deployment.rig.open().list()) == 1

    def test_principal_cannot_revoke_itself_or_others(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        app = issue_principal(deployment.rig.open(), scopes=all_scopes())
        resp = deployment.client.post(
            f"{PRINCIPALS}/{app.record.id}/revoke", headers=app.bearer_headers()
        )
        assert resp.status_code == 403, resp.text
        assert deployment.stored(app.record.id).status == "active"


# --- revoke (REQ-556, REQ-573) ----------------------------------------------------------


class TestRevoke:
    def test_revoke_takes_effect_on_the_next_principal_request(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, operator = deployment.operator()
        issued = _issue(deployment, headers)
        assert deployment.probe_status(issued.bearer()) == 200

        resp = deployment.client.post(
            f"{PRINCIPALS}/{issued.principal_id}/revoke", headers=headers
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["principal"]["status"] == "revoked"
        record = deployment.stored(issued.principal_id)
        assert record.status == "revoked"
        assert record.revoked_by == operator
        assert deployment.probe_status(issued.bearer()) == 401

    def test_revoke_writes_an_applied_audit_row(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, operator = deployment.operator()
        issued = _issue(deployment, headers)

        deployment.client.post(f"{PRINCIPALS}/{issued.principal_id}/revoke", headers=headers)

        applied = [
            r
            for r in deployment.rows()
            if r.outcome == "applied" and issued.principal_id in r.target
        ]
        issue_row, revoke_row = applied
        assert revoke_row.operation != issue_row.operation
        assert revoke_row.actor_did == operator

    def test_revoke_unknown_is_404_and_audited(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        resp = deployment.client.post(f"{PRINCIPALS}/{'0' * 32}/revoke", headers=headers)
        assert resp.status_code == 404, resp.text
        assert [r.outcome for r in deployment.rows()] == ["denied"]

    def test_second_revoke_is_409(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        issued = _issue(deployment, headers)
        path = f"{PRINCIPALS}/{issued.principal_id}/revoke"
        assert deployment.client.post(path, headers=headers).status_code == 200

        resp = deployment.client.post(path, headers=headers)

        assert resp.status_code == 409, resp.text

    def test_revoked_principal_is_listed_as_revoked(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        issued = _issue(deployment, headers)
        deployment.client.post(f"{PRINCIPALS}/{issued.principal_id}/revoke", headers=headers)

        (row,) = deployment.client.get(PRINCIPALS, headers=headers).json()["principals"]

        assert row["status"] == "revoked"


# --- enterprise: pending + approval row bound to the hash (REQ-553) ---------------------


class TestEnterpriseIssue:
    def test_issue_is_pending_with_an_approval_row_bound_to_the_content_hash(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        headers, operator = deployment.operator()

        issued = _issue(deployment, headers)

        assert issued.response["status"] == "pending"
        assert BEARER_RE.match(issued.secret)
        record = deployment.stored(issued.principal_id)
        assert record.status == "pending"
        assert record.created_by == operator
        (row,) = deployment.approval_rows()
        assert row.id == issued.approval_id
        assert row.tool == PRINCIPAL_ISSUE_TOOL
        assert row.status == "pending"
        assert row.call_hash == record.content_hash()
        assert row.agent_did == OLIVIA
        assert set(row.arguments) == {"principal_id", "name", "scopes"}
        assert all(isinstance(v, str) for v in row.arguments.values())
        assert row.arguments["principal_id"] == issued.principal_id
        assert row.arguments["name"] == "billing-app"
        assert row.arguments["scopes"] == canonical_json({str(Scope.INBOX_SEND): [OLIVIA]}).decode(
            "utf-8"
        )
        assert record.secret_digest not in row.model_dump_json()

    def test_pending_principal_token_is_401_at_the_middleware(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        headers, _ = deployment.operator()

        issued = _issue(deployment, headers)

        assert deployment.probe_status(issued.bearer()) == 401

    def test_approving_the_row_activates_the_principal(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        requester, _ = deployment.operator("req@example.com")
        issued = _issue(deployment, requester)
        approver, approver_did = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{issued.approval_id}/approve", headers=approver
        )

        assert resp.status_code == 200, resp.text
        record = deployment.stored(issued.principal_id)
        assert record.status == "active"
        assert record.approved_by == approver_did
        assert deployment.approval(issued.approval_id).status == "approved"
        assert deployment.probe_status(issued.bearer()) == 200

    def test_enterprise_requester_may_approve_their_own_request(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        # The two-person rule is federal only (REQ-554).
        deployment = deploy("enterprise")
        headers, operator = deployment.operator()
        issued = _issue(deployment, headers)

        resp = deployment.client.post(
            f"/api/approvals/{issued.approval_id}/approve", headers=headers
        )

        assert resp.status_code == 200, resp.text
        assert deployment.stored(issued.principal_id).approved_by == operator

    def test_record_edited_after_the_request_is_409_and_stays_pending(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        headers, _ = deployment.operator()
        issued = _issue(deployment, headers)
        _tamper_stored_scopes(deployment.rig, issued.principal_id)
        approver, _ = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{issued.approval_id}/approve", headers=approver
        )

        assert resp.status_code == 409, resp.text
        assert deployment.stored(issued.principal_id).status == "pending"
        assert deployment.approval(issued.approval_id).status == "pending"
        assert deployment.approval(issued.approval_id).grant is None
        assert deployment.probe_status(issued.bearer()) == 401
        denied = [r for r in deployment.rows("approval.") if r.outcome == "denied"]
        assert len(denied) == 1, deployment.built.worm.written

    def test_deny_revokes_the_pending_principal(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy("enterprise")
        headers, _ = deployment.operator()
        issued = _issue(deployment, headers)
        approver, approver_did = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{issued.approval_id}/deny", headers=approver
        )

        assert resp.status_code == 200, resp.text
        record = deployment.stored(issued.principal_id)
        assert record.status == "revoked"
        assert record.revoked_by == approver_did
        assert deployment.approval(issued.approval_id).status == "denied"
        assert deployment.probe_status(issued.bearer()) == 401

    def test_revoking_a_pending_principal_then_approving_does_not_activate_it(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        headers, _ = deployment.operator()
        issued = _issue(deployment, headers)
        assert (
            deployment.client.post(
                f"{PRINCIPALS}/{issued.principal_id}/revoke", headers=headers
            ).status_code
            == 200
        )
        approver, _ = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{issued.approval_id}/approve", headers=approver
        )

        assert resp.status_code == 409, resp.text
        assert deployment.stored(issued.principal_id).status == "revoked"
        assert deployment.probe_status(issued.bearer()) == 401

    def test_approval_row_for_a_missing_principal_is_409_not_a_crash(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        asyncio.run(
            deployment.approvals.create(
                PendingApproval(
                    id="forged",
                    agent_did=OLIVIA,
                    tool=PRINCIPAL_ISSUE_TOOL,
                    call_hash="0" * 64,
                    arguments={"principal_id": "f" * 32, "name": "ghost", "scopes": "{}"},
                )
            )
        )
        headers, _ = deployment.operator()

        resp = deployment.client.post("/api/approvals/forged/approve", headers=headers)

        assert resp.status_code == 409, resp.text
        assert deployment.approval("forged").status == "pending"


# --- federal: two-person rule (REQ-554) ---------------------------------------------------


class TestFederalIssue:
    def test_approver_equal_to_requester_is_403_audited_and_stays_pending(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("federal")
        headers, _ = deployment.operator()
        issued = _issue(deployment, headers)
        assert issued.response["status"] == "pending"

        resp = deployment.client.post(
            f"/api/approvals/{issued.approval_id}/approve", headers=headers
        )

        assert resp.status_code == 403, resp.text
        assert deployment.stored(issued.principal_id).status == "pending"
        assert deployment.approval(issued.approval_id).status == "pending"
        denied = [r for r in deployment.rows("approval.") if r.outcome == "denied"]
        assert len(denied) == 1, deployment.built.worm.written

    def test_a_different_operator_approves_and_the_principal_works(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("federal")
        requester, _ = deployment.operator("req@example.com")
        issued = _issue(deployment, requester)
        assert _signed_probe(deployment, issued) == 401
        approver, approver_did = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{issued.approval_id}/approve", headers=approver
        )

        assert resp.status_code == 200, resp.text
        record = deployment.stored(issued.principal_id)
        assert record.status == "active"
        assert record.approved_by == approver_did
        assert _signed_probe(deployment, issued) == 200

    def test_federal_self_approval_then_other_operator_succeeds(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("federal")
        requester, _ = deployment.operator("req@example.com")
        issued = _issue(deployment, requester)
        path = f"/api/approvals/{issued.approval_id}/approve"
        assert deployment.client.post(path, headers=requester).status_code == 403
        approver, _ = deployment.operator("app@example.com")

        resp = deployment.client.post(path, headers=approver)

        assert resp.status_code == 200, resp.text
        assert deployment.stored(issued.principal_id).status == "active"
