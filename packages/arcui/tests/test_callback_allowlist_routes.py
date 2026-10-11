"""SPEC-088 T-1367: callback allowlist management routes and the approval special case.

COMP-018 routes (``arcui.routes.work_events_callbacks`` or any module ``create_app`` mounts):

- ``GET  /api/work-events/callbacks``               -> 200 ``{"callbacks": [<entry row>, ...]}``
- ``POST /api/work-events/callbacks``  body ``{"origin": str, "allow_private": bool = false,
  "clearance": str = "UNCLASSIFIED"}`` (strict, ``extra="forbid"``)
  -> 201 ``{"callback": <entry row>, "status": "active" | "pending"}`` plus ``"approval_id"``
  when the tier keeps the entry pending;
- ``POST /api/work-events/callbacks/{id}/remove``   -> 200 ``{"callback": <entry row>}``.

An entry row is ``AllowlistEntry.to_dict()``: ``id, origin, allow_private, clearance,
status, created_by, approved_by, created_at``.

Seam: ``create_app(..., callback_allowlist_factory: Callable[[], CallbackAllowlistStore])``
sets ``app.state.callback_allowlist_factory`` (beside SPEC-087's ``principal_store_factory``);
the tier is ``app.state.principals_config.tier``.

Contract choices pinned here:

- signed-in OPERATOR session only: viewer session, static viewer token and static operator
  token answer 403 ``operator_session_required``; an app principal is refused by the
  middleware (no ``APP_ROUTES`` row for these paths) — 403 either way, nothing stored;
- bad origin (path, userinfo, query) -> 400 ``invalid_origin``; malformed body -> 400
  ``invalid_callback``; duplicate live origin -> 409 ``duplicate_origin``; unknown id on
  remove -> 404 ``unknown_entry``; second remove -> 409 ``already_removed``;
- account authority absent (no factory) -> 503 ``account_authority_unavailable``;
- enterprise/federal add -> pending + one ``PendingApproval(tool="work_event_callback",
  call_hash=<entry content hash>, arguments={"entry_id", "origin", "allow_private",
  "clearance"} all str)``; ``POST /api/approvals/{id}/approve`` activates it only while the
  record still hashes to the row's call_hash (409 otherwise), federal refuses approver ==
  requester (403 ``second_operator_required``); deny removes the pending entry;
- every change attempt writes one ``ui.mutation`` row whose operation starts with
  ``work_event.callback_allowlist.`` (approval decisions keep ``approval.*``).

Requirements: REQ-625, REQ-631. Real ``create_app`` + ``AuthMiddleware``, real ``UserStore``,
real anchored ``CallbackAllowlistStore`` and ``PrincipalStore``, production
``ApprovalStore`` over arcstore's ``FakeBackend``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from arcstore.approvals import ApprovalStore, PendingApproval
from arcstore.backends.memory import FakeBackend
from arctrust import FileJournalAnchor, InProcessSigner, OperatorKey, default_operator_key_path
from arctrust.audit import AuditEvent
from arctrust.sealed_state import SealedAuthorityState
from arctrust.users import OPERATOR, VIEWER
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    VIEWER_TOKEN,
    DurableSink,
    PrincipalApp,
    PrincipalRig,
    RecordingSink,
    SecretBoxCipher,
    SpyWorm,
    agent_did,
    all_scopes,
    build_principal_app,
    issue_principal,
    principal_rig,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

CALLBACKS = "/api/work-events/callbacks"
CALLBACK_TOOL = "work_event_callback"
ORIGIN = "https://hooks.example.com"
STORE_ACTOR = "did:arc:test:operator/callback-store-actor"
STATIC_OPERATOR = {"Authorization": f"Bearer {OPERATOR_TOKEN}"}
STATIC_VIEWER = {"Authorization": f"Bearer {VIEWER_TOKEN}"}
OLIVIA = agent_did()
_ANCHOR_SIGNER = InProcessSigner(bytes(range(32)))


# --- the callback authority rig ------------------------------------------------------


def _private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


@dataclass
class CallbackRig:
    path: Path
    anchor: FileJournalAnchor
    tier: str
    cipher: SecretBoxCipher = field(default_factory=SecretBoxCipher)
    audit: RecordingSink = field(default_factory=RecordingSink)
    strict: DurableSink = field(default_factory=DurableSink)

    def open(self) -> Any:
        from arctrust.callback_allowlist import CallbackAllowlistStore

        return CallbackAllowlistStore(
            self.path,
            anchor=self.anchor,
            cipher=self.cipher,
            audit_sink=self.audit,
            strict_audit_sink=self.strict,
            actor_did=STORE_ACTOR,
            tier=self.tier,
        )

    def entries(self) -> list[Any]:
        return list(self.open().list())

    def entry(self, entry_id: str) -> Any:
        (found,) = [e for e in self.entries() if e.id == entry_id]
        return found


def _callback_rig(root: Path, tier: str) -> CallbackRig:
    return CallbackRig(
        path=_private_dir(root / "callbacks") / "callbacks.json",
        anchor=FileJournalAnchor(
            _private_dir(root / "callbacks-anchor"), scope="test/callbacks", signer=_ANCHOR_SIGNER
        ),
        tier=tier,
    )


@dataclass
class _StoreSink:
    events: list[AuditEvent] = field(default_factory=list)

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


# --- the deployment under test -------------------------------------------------------


@dataclass
class _Deployment:
    app: Any
    auth: Any
    worm: SpyWorm
    users: Callable[[], Any]
    callbacks: CallbackRig
    principals: PrincipalRig
    approvals: ApprovalStore
    client: TestClient

    def _sign_in(self, email: str, roles: tuple[str, ...]) -> tuple[dict[str, str], str]:
        user = self.users().add(email, "correct-horse-battery", roles=roles)
        role = "operator" if user.is_operator else "viewer"
        session = self.auth.sessions.issue(email=user.email, did=user.did, role=role)
        return {"Authorization": f"Bearer {session.token}"}, user.did

    def operator(self, email: str = "ada@example.com") -> tuple[dict[str, str], str]:
        return self._sign_in(email, (OPERATOR,))

    def viewer(self, email: str = "vic@example.com") -> tuple[dict[str, str], str]:
        return self._sign_in(email, (VIEWER,))

    def approval_rows(self) -> list[PendingApproval]:
        return [r for r in asyncio.run(self.approvals.list()) if r.tool == CALLBACK_TOOL]

    def approval(self, approval_id: str) -> PendingApproval:
        row = asyncio.run(self.approvals.get(approval_id))
        assert row is not None, approval_id
        return row

    def rows(self, prefix: str = "work_event.callback_allowlist.") -> list[Any]:
        return [r for r in self.worm.written if r.operation.startswith(prefix)]

    def add(self, headers: dict[str, str], **body: Any) -> Any:
        return self.client.post(CALLBACKS, json={"origin": ORIGIN, **body}, headers=headers)


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path))
    # The approvals approve route signs its grant with the on-box operator key.
    OperatorKey.load(default_operator_key_path(tmp_path), generate_if_absent=True)
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def deploy(tmp_path: Path, users: Any) -> Callable[..., _Deployment]:
    def _make(tier: str = "personal", *, wire_callbacks: bool = True) -> _Deployment:
        from arcui.auth import AuthConfig
        from arcui.principals import PrincipalsConfig
        from arcui.server import create_app

        root = tmp_path / tier
        callbacks = _callback_rig(root / "authority", tier)
        principals = principal_rig(root / "authority")
        auth = AuthConfig({"operator_token": OPERATOR_TOKEN, "viewer_token": VIEWER_TOKEN})
        seam: dict[str, Any] = {}
        if wire_callbacks:
            seam["callback_allowlist_factory"] = callbacks.open
        app = create_app(
            auth_config=auth,
            user_store_factory=users,
            team_root=write_team(root / "team", {"olivia": OLIVIA}),
            data_dir=root / "arcui-data",
            principal_store_factory=principals.factory,
            principals_config=PrincipalsConfig(tier=tier),
            **seam,
        )
        worm = SpyWorm()
        app.state.audit_worm = worm
        backend = FakeBackend()
        asyncio.run(backend.start())
        approvals = ApprovalStore(backend, sink=_StoreSink())
        app.state.approval_store = approvals
        return _Deployment(
            app=app,
            auth=auth,
            worm=worm,
            users=users,
            callbacks=callbacks,
            principals=principals,
            approvals=approvals,
            client=TestClient(app, raise_server_exceptions=False),
        )

    return _make


def _tamper_origin(rig: CallbackRig, entry_id: str, origin: str) -> None:
    """A privileged writer edits the pending record through the real sealed state.

    The edit reads back as authentic; only the approval row's content hash catches it.
    """
    state = SealedAuthorityState(
        rig.path,
        anchor=rig.anchor,
        cipher=rig.cipher,
        audit_sink=rig.audit,
        strict_audit_sink=rig.strict,
        record_name="callbacks",
        actor_did=STORE_ACTOR,
    )
    payload = state.load()
    assert payload is not None
    (record,) = [e for e in payload["entries"] if e["id"] == entry_id]
    record["origin"] = origin
    state.save(payload)


# --- fixture smoke: proves the harness, not the feature ------------------------------


class TestFixtureSmoke:
    def test_operator_session_is_accepted_by_an_existing_operator_route(
        self, tmp_path: Path, users: Any
    ) -> None:
        built: PrincipalApp = build_principal_app(
            tmp_path / "smoke",
            principal_store_factory=principal_rig(tmp_path / "p").factory,
            users=users,
        )
        token, _ = built.sign_in("ada@example.com", (OPERATOR,))
        client = TestClient(built.app, raise_server_exceptions=False)
        resp = client.get("/api/principals", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text


# --- personal: active at once ----------------------------------------------------------


class TestPersonal:
    def test_add_is_active_at_once_and_names_the_operator(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, operator = deployment.operator()

        resp = deployment.add(headers)

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "active"
        assert "approval_id" not in body
        row = body["callback"]
        assert row["origin"] == ORIGIN
        assert row["allow_private"] is False
        assert row["clearance"] == "UNCLASSIFIED"
        stored = deployment.callbacks.entry(row["id"])
        assert stored.status == "active"
        assert stored.created_by == operator
        assert deployment.approval_rows() == []

    def test_add_with_allow_private_and_clearance(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()

        resp = deployment.add(
            headers, origin="https://10.0.0.5:9000", allow_private=True, clearance="CUI"
        )

        assert resp.status_code == 201, resp.text
        stored = deployment.callbacks.entry(resp.json()["callback"]["id"])
        assert stored.allow_private is True
        assert stored.clearance.name == "CUI"

    def test_list_returns_every_entry(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        deployment.add(headers)
        deployment.add(headers, origin="https://other.example.com")

        resp = deployment.client.get(CALLBACKS, headers=headers)

        assert resp.status_code == 200, resp.text
        rows = resp.json()["callbacks"]
        assert sorted(r["origin"] for r in rows) == [
            "https://hooks.example.com",
            "https://other.example.com",
        ]
        assert set(rows[0]) >= {
            "id",
            "origin",
            "allow_private",
            "clearance",
            "status",
            "created_by",
            "approved_by",
            "created_at",
        }

    def test_remove_takes_effect_in_the_authority_at_once(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        entry_id = deployment.add(headers).json()["callback"]["id"]

        resp = deployment.client.post(f"{CALLBACKS}/{entry_id}/remove", headers=headers)

        assert resp.status_code == 200, resp.text
        assert resp.json()["callback"]["status"] == "removed"
        assert deployment.callbacks.entry(entry_id).status == "removed"

    def test_remove_unknown_is_404_and_second_remove_is_409(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        entry_id = deployment.add(headers).json()["callback"]["id"]
        deployment.client.post(f"{CALLBACKS}/{entry_id}/remove", headers=headers)

        twice = deployment.client.post(f"{CALLBACKS}/{entry_id}/remove", headers=headers)
        unknown = deployment.client.post(f"{CALLBACKS}/{'f' * 32}/remove", headers=headers)

        assert (twice.status_code, twice.json()["error"]) == (409, "already_removed")
        assert (unknown.status_code, unknown.json()["error"]) == (404, "unknown_entry")

    @pytest.mark.parametrize(
        "origin",
        [
            "https://hooks.example.com/callbacks",
            "https://user:pw@hooks.example.com",
            "https://hooks.example.com?x=1",
            "ftp://hooks.example.com",
        ],
    )
    def test_bad_origin_is_400_audited_and_writes_nothing(
        self, deploy: Callable[..., _Deployment], origin: str
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()

        resp = deployment.add(headers, origin=origin)

        assert (resp.status_code, resp.json()["error"]) == (400, "invalid_origin")
        assert deployment.callbacks.entries() == []
        assert [r.outcome for r in deployment.rows()] == ["denied"]

    @pytest.mark.parametrize(
        "body",
        [{"origin": ORIGIN, "extra": 1}, {"origin": 5}, {}, {"origin": ORIGIN, "clearance": "X"}],
    )
    def test_malformed_body_is_400(
        self, deploy: Callable[..., _Deployment], body: dict[str, Any]
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()

        resp = deployment.client.post(CALLBACKS, json=body, headers=headers)

        assert resp.status_code == 400, resp.text
        assert deployment.callbacks.entries() == []

    def test_duplicate_live_origin_is_409(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        headers, _ = deployment.operator()
        deployment.add(headers)

        resp = deployment.add(headers, origin="HTTPS://hooks.example.com:443")

        assert (resp.status_code, resp.json()["error"]) == (409, "duplicate_origin")
        assert len(deployment.callbacks.entries()) == 1

    def test_add_and_remove_each_write_one_applied_audit_row(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy()
        headers, operator = deployment.operator()
        entry_id = deployment.add(headers).json()["callback"]["id"]
        deployment.client.post(f"{CALLBACKS}/{entry_id}/remove", headers=headers)

        rows = deployment.rows()

        assert [r.outcome for r in rows] == ["applied", "applied"]
        assert all(entry_id in r.target for r in rows)
        assert {r.actor_did for r in rows} == {operator}


# --- who may call ----------------------------------------------------------------------


class TestCallers:
    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_viewer_session_is_403_and_audited(
        self, deploy: Callable[..., _Deployment], method: str
    ) -> None:
        deployment = deploy()
        headers, _ = deployment.viewer()

        resp = deployment.client.request(
            method,
            CALLBACKS,
            json={"origin": ORIGIN} if method == "POST" else None,
            headers=headers,
        )

        assert (resp.status_code, resp.json().get("error")) == (403, "operator_session_required")
        assert deployment.callbacks.entries() == []
        if method == "POST":
            assert [r.outcome for r in deployment.rows()] == ["denied"]

    @pytest.mark.parametrize(
        "headers", [STATIC_VIEWER, STATIC_OPERATOR], ids=["viewer", "operator"]
    )
    def test_static_tokens_are_403(
        self, deploy: Callable[..., _Deployment], headers: dict[str, str]
    ) -> None:
        deployment = deploy()

        add = deployment.add(headers)
        listed = deployment.client.get(CALLBACKS, headers=headers)

        assert add.status_code == 403, add.text
        assert listed.status_code == 403, listed.text
        assert deployment.callbacks.entries() == []

    def test_viewer_cannot_remove(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy()
        operator, _ = deployment.operator()
        entry_id = deployment.add(operator).json()["callback"]["id"]
        viewer, _ = deployment.viewer()

        resp = deployment.client.post(f"{CALLBACKS}/{entry_id}/remove", headers=viewer)

        assert resp.status_code == 403, resp.text
        assert deployment.callbacks.entry(entry_id).status == "active"

    @pytest.mark.parametrize("path_suffix", ["", "/{id}/remove"])
    def test_app_principal_with_every_scope_is_refused(
        self, deploy: Callable[..., _Deployment], path_suffix: str
    ) -> None:
        """No APP_ROUTES row: the middleware refuses the app before any handler runs."""
        deployment = deploy()
        operator, _ = deployment.operator()
        entry_id = deployment.add(operator).json()["callback"]["id"]
        app = issue_principal(deployment.principals.open(), scopes=all_scopes())

        resp = deployment.client.post(
            CALLBACKS + path_suffix.format(id=entry_id),
            json={"origin": "https://evil.example.com"},
            headers=app.bearer_headers(),
        )
        listed = deployment.client.get(CALLBACKS, headers=app.bearer_headers())

        assert resp.status_code == 403, resp.text
        assert listed.status_code == 403, listed.text
        assert [(e.origin, e.status) for e in deployment.callbacks.entries()] == [
            (ORIGIN, "active")
        ]

    def test_absent_account_authority_is_503_and_writes_nothing(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy(wire_callbacks=False)
        headers, _ = deployment.operator()

        add = deployment.add(headers)
        listed = deployment.client.get(CALLBACKS, headers=headers)

        assert (add.status_code, add.json()["error"]) == (503, "account_authority_unavailable")
        assert listed.status_code == 503, listed.text
        assert deployment.callbacks.anchor.latest() is None


# --- enterprise / federal: pending + approval row bound to the hash ---------------------


class TestSecondStep:
    @pytest.mark.parametrize("tier", ["enterprise", "federal"])
    def test_add_is_pending_with_an_approval_row_bound_to_the_entry_hash(
        self, deploy: Callable[..., _Deployment], tier: str
    ) -> None:
        deployment = deploy(tier)
        headers, operator = deployment.operator()

        resp = deployment.add(headers)

        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "pending"
        record = deployment.callbacks.entry(body["callback"]["id"])
        assert record.status == "pending"
        assert record.created_by == operator
        (row,) = deployment.approval_rows()
        assert row.id == body["approval_id"]
        assert row.tool == CALLBACK_TOOL
        assert row.status == "pending"
        assert row.call_hash == record.content_hash()
        assert row.arguments["entry_id"] == record.id
        assert row.arguments["origin"] == ORIGIN
        assert all(isinstance(v, str) for v in row.arguments.values())

    def test_approving_the_row_activates_the_entry(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        requester, _ = deployment.operator("req@example.com")
        body = deployment.add(requester).json()
        approver, approver_did = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{body['approval_id']}/approve", headers=approver
        )

        assert resp.status_code == 200, resp.text
        record = deployment.callbacks.entry(body["callback"]["id"])
        assert record.status == "active"
        assert record.approved_by == approver_did
        assert deployment.approval(body["approval_id"]).status == "approved"

    def test_federal_requester_cannot_approve_their_own_request(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("federal")
        headers, _ = deployment.operator()
        body = deployment.add(headers).json()

        resp = deployment.client.post(
            f"/api/approvals/{body['approval_id']}/approve", headers=headers
        )

        assert (resp.status_code, resp.json()["error"]) == (403, "second_operator_required")
        assert deployment.callbacks.entry(body["callback"]["id"]).status == "pending"
        assert deployment.approval(body["approval_id"]).status == "pending"

    def test_federal_second_operator_activates(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy("federal")
        requester, _ = deployment.operator("req@example.com")
        body = deployment.add(requester).json()
        approver, _ = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{body['approval_id']}/approve", headers=approver
        )

        assert resp.status_code == 200, resp.text
        assert deployment.callbacks.entry(body["callback"]["id"]).status == "active"

    def test_record_edited_after_the_request_is_409_and_stays_pending(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        requester, _ = deployment.operator("req@example.com")
        body = deployment.add(requester).json()
        entry_id = body["callback"]["id"]
        _tamper_origin(deployment.callbacks, entry_id, "https://evil.example.com")
        approver, _ = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{body['approval_id']}/approve", headers=approver
        )

        assert resp.status_code == 409, resp.text
        assert deployment.callbacks.entry(entry_id).status == "pending"
        assert deployment.approval(body["approval_id"]).status == "pending"

    def test_deny_removes_the_pending_entry(self, deploy: Callable[..., _Deployment]) -> None:
        deployment = deploy("enterprise")
        requester, _ = deployment.operator("req@example.com")
        body = deployment.add(requester).json()
        approver, _ = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{body['approval_id']}/deny", headers=approver
        )

        assert resp.status_code == 200, resp.text
        assert deployment.callbacks.entry(body["callback"]["id"]).status == "removed"
        assert deployment.approval(body["approval_id"]).status == "denied"

    def test_removing_a_pending_entry_then_approving_does_not_activate_it(
        self, deploy: Callable[..., _Deployment]
    ) -> None:
        deployment = deploy("enterprise")
        requester, _ = deployment.operator("req@example.com")
        body = deployment.add(requester).json()
        entry_id = body["callback"]["id"]
        deployment.client.post(f"{CALLBACKS}/{entry_id}/remove", headers=requester)
        approver, _ = deployment.operator("app@example.com")

        resp = deployment.client.post(
            f"/api/approvals/{body['approval_id']}/approve", headers=approver
        )

        assert resp.status_code == 409, resp.text
        assert deployment.callbacks.entry(entry_id).status == "removed"
