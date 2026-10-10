"""SPEC-087 T-1277: app route table, deny-by-default and scope checks (COMP-005).

REQ-555 (authenticate every principal request), REQ-557 (deny by default: only
``APP_ROUTES`` are reachable, hard-denied prefixes never are), REQ-558 (scope
and agent binding), REQ-559 (signed requests at federal / when required),
REQ-572 (authority unreachable fails closed for apps only), REQ-578 (every
refusal is audited).

Everything runs through the REAL ``create_app`` build and its real
``AuthMiddleware``: a real ``UserStore`` and a real ``PrincipalStore`` (see
``principal_fixtures``), credentials minted by ``arctrust.principal_auth``,
and a real ``team/`` roster so agent labels resolve to DIDs. The only test
seam is a probe route mounted ahead of the real handler: it proves the
middleware let a request through, and what it bound, without depending on
handler rewiring that later tasks (COMP-008) own.

Agent identity contract pinned here: principal scopes bind agent DIDs; the
inbox row's path carries the roster label, which the middleware resolves to the
DID through the roster before checking the scope's agent set.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest
from arctrust.principals import Scope
from arctrust.users import OPERATOR
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    VIEWER_TOKEN,
    WILDCARD,
    PrincipalApp,
    PrincipalRig,
    agent_did,
    all_scopes,
    build_principal_app,
    build_session_only_app,
    issue_principal,
    principal_rig,
    signed_headers,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route
from starlette.testclient import TestClient

OLIVIA = agent_did()
BOB = agent_did()
INBOX_OLIVIA = "/api/agents/olivia/inbox"
INBOX_BOB = "/api/agents/bob/inbox"
INBOX_PATTERN = "/api/agents/{id}/inbox"
SCOPE_PROBE = "/api/tasks/{id:path}/approve"
SCOPE_PROBE_PATH = "/api/tasks/probe-task/approve"

# The design note's hard-denied list (SDD COMP-005 + /api/me for delegations).
HARD_DENIED = (
    "/api/approvals",
    "/api/standing-grants",
    "/api/principals",
    "/api/users",
    "/api/trust",
    "/api/me",
)

# SDD COMP-005: the ONLY (method, route template) pairs an app can reach today.
EXPECTED_APP_ROUTES = {
    ("POST", "/api/workflows/{id}/run"),
    ("GET", "/api/workflow-runs/{id}"),
    ("GET", "/api/runs/{run_id}/timeline"),
    ("POST", "/api/workflow-tasks/{id:path}/gate"),
    ("POST", "/api/tasks/{id:path}/approve"),
    ("POST", "/api/tasks/{id:path}/reject"),
    ("POST", "/api/team/tasks"),
    ("POST", "/api/agents/{id}/inbox"),
}

_PARAM = re.compile(r"\{[A-Za-z_]+(?::[a-z]+)?\}")
_SKIPPED_METHODS = {"HEAD", "OPTIONS"}


# --- fixtures --------------------------------------------------------------------


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def rig(tmp_path: Path) -> PrincipalRig:
    return principal_rig(tmp_path / "authority")


@pytest.fixture
def team(tmp_path: Path) -> Path:
    return write_team(tmp_path / "team", {"olivia": OLIVIA, "bob": BOB})


def _app(tmp_path: Path, rig: PrincipalRig, users: Any, team: Path, **kw: Any) -> PrincipalApp:
    kw.setdefault("tier", "personal")
    return build_principal_app(
        tmp_path, principal_store_factory=rig.factory, users=users, team_root=team, **kw
    )


def _client(built: PrincipalApp) -> TestClient:
    return TestClient(built.app, raise_server_exceptions=False)


def _refusal_rows(built: PrincipalApp) -> list[Any]:
    return built.worm.denied()


# --- fixture smoke: proves the harness, not the feature ----------------------------


class TestFixtureSmoke:
    """These pass today; they prove a RED below is the missing feature, not the rig."""

    def test_principal_store_rig_persists_across_reopen(self, rig: PrincipalRig) -> None:
        assert rig.open().list() == []
        assert rig.factory().list() == []
        assert rig.path.parent.is_dir()

    def test_issued_principal_credentials_verify_against_the_real_store(
        self, rig: PrincipalRig
    ) -> None:
        from datetime import UTC, datetime

        from arctrust.principal_auth import (
            SignedRequest,
            parse_bearer,
            verify_bearer,
            verify_signed,
        )
        from arctrust.replay import ReplayCache

        issued = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        stored = rig.factory().get(issued.record.id)
        assert stored is not None and stored.status == "active"

        principal_id, secret = parse_bearer(issued.bearer)
        now = datetime.now(UTC)
        assert verify_bearer(rig.open().get(principal_id), secret, now=now).did == issued.did

        headers = signed_headers(issued, method="POST", path=INBOX_OLIVIA, body=b"{}")
        envelope = SignedRequest.from_headers(headers, "POST", INBOX_OLIVIA, "", b"{}")
        assert verify_signed(stored, envelope, ReplayCache(window_seconds=300), now=now).did == (
            issued.did
        )

    def test_session_only_app_serves_a_signed_in_operator(
        self, tmp_path: Path, users: Any, team: Path
    ) -> None:
        built = build_session_only_app(tmp_path, users=users, team_root=team)
        token, did = built.sign_in("boss@example.com", (OPERATOR,))
        resp = _client(built).get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["did"] == did

    def test_probe_route_is_reached_by_a_session(
        self, tmp_path: Path, users: Any, team: Path
    ) -> None:
        built = build_session_only_app(tmp_path, users=users, team_root=team)
        built.add_probe(INBOX_PATTERN)
        token, _ = built.sign_in("boss@example.com", (OPERATOR,))
        resp = _client(built).post(
            INBOX_OLIVIA, content=b'{"x":1}', headers={"Authorization": f"Bearer {token}"}
        )
        assert resp.status_code == 200, resp.text
        assert built.probes[-1]["body"] == '{"x":1}'

    def test_roster_resolves_labels_to_dids(self, tmp_path: Path, users: Any, team: Path) -> None:
        built = build_session_only_app(tmp_path, users=users, team_root=team)
        rows = {row.agent_id: row.did for row in built.app.state.roster_provider()}
        assert rows["olivia"] == OLIVIA
        assert rows["bob"] == BOB

    def test_route_walk_is_not_vacuous(self, tmp_path: Path, users: Any, team: Path) -> None:
        built = build_session_only_app(tmp_path, users=users, team_root=team)
        endpoints = _api_endpoints(built.app)
        assert len(endpoints) > 200
        assert ("POST", INBOX_PATTERN) in {(m, t) for m, t, _ in endpoints}


# --- route walk (REQ-557) ------------------------------------------------------------


def _api_endpoints(app: Any) -> list[tuple[str, str, str]]:
    """Every (method, template, concrete path) under /api/ the middleware guards."""
    from arcui.auth import _UNAUTHENTICATED_PATHS

    found: list[tuple[str, str, str]] = []

    def walk(routes: list[Any], prefix: str) -> None:
        for route in routes:
            if isinstance(route, Mount):
                walk(list(getattr(route, "routes", []) or []), prefix + route.path)
                continue
            if not isinstance(route, Route):
                continue
            template = prefix + route.path
            concrete = _PARAM.sub("probe-1", template)
            if not concrete.startswith("/api/") or concrete in _UNAUTHENTICATED_PATHS:
                continue
            for method in sorted((route.methods or set()) - _SKIPPED_METHODS):
                found.append((method, template, concrete))

    walk(list(app.routes), "")
    return found


def _hard_denied(path: str) -> bool:
    return any(path == p or path.startswith(p + "/") for p in HARD_DENIED)


class TestDenyByDefault:
    def test_app_route_table_is_exactly_the_sdd_rows(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        from arcui.principals import match_app_route

        built = _app(tmp_path, rig, users, team)
        reachable = {
            (method, template)
            for method, template, concrete in _api_endpoints(built.app)
            if match_app_route(method, concrete) is not None and not _hard_denied(concrete)
        }
        assert reachable == EXPECTED_APP_ROUTES

    def test_hard_denied_prefixes_cover_the_design_list(self) -> None:
        from arcui.principals import HARD_DENIED_PREFIXES

        assert set(HARD_DENIED) <= set(HARD_DENIED_PREFIXES)

    def test_every_route_outside_app_routes_refused_even_with_all_scopes(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        from arcui.principals import match_app_route

        built = _app(tmp_path, rig, users, team, rate_per_minute=100_000)
        app_principal = issue_principal(rig.open(), scopes=all_scopes((WILDCARD,)))
        client = _client(built)
        failures: list[tuple[str, str, int, int]] = []
        checked = 0
        for method, _template, concrete in _api_endpoints(built.app):
            if match_app_route(method, concrete) is not None and not _hard_denied(concrete):
                continue
            before = len(_refusal_rows(built))
            resp = client.request(method, concrete, headers=app_principal.bearer_headers())
            rows = len(_refusal_rows(built)) - before
            checked += 1
            if resp.status_code != 403 or rows != 1:
                failures.append((method, concrete, resp.status_code, rows))
        assert checked > 200
        assert failures == [], f"{len(failures)} routes not refused with one audit row"

    @pytest.mark.parametrize("prefix", HARD_DENIED)
    @pytest.mark.parametrize("method", ["GET", "POST"])
    def test_hard_denied_prefix_refused_with_all_scopes(
        self,
        tmp_path: Path,
        rig: PrincipalRig,
        users: Any,
        team: Path,
        prefix: str,
        method: str,
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        app_principal = issue_principal(rig.open(), scopes=all_scopes((WILDCARD,)))
        client = _client(built)
        for path in (prefix, prefix + "/anything", prefix + "/x/approve"):
            before = len(_refusal_rows(built))
            resp = client.request(method, path, headers=app_principal.bearer_headers())
            assert resp.status_code == 403, (method, path, resp.text)
            assert len(_refusal_rows(built)) - before == 1

    @pytest.mark.parametrize(
        "path", ["/api/approvals/a1/approve", "/api/workflows/wf-1/request-signature"]
    )
    def test_hard_deny_wins_over_a_matching_table_row(
        self,
        tmp_path: Path,
        rig: PrincipalRig,
        users: Any,
        team: Path,
        monkeypatch: pytest.MonkeyPatch,
        path: str,
    ) -> None:
        """A later spec that adds a row covering a hard-denied path (by a broad
        regex or by mistake) still cannot open it to apps."""
        import arcui.principals as principals_module

        widened = (
            *principals_module.APP_ROUTES,
            principals_module.AppRoute("POST", re.compile(r"/api/.+"), Scope.RUNS_READ),
        )
        monkeypatch.setattr(principals_module, "APP_ROUTES", widened)
        built = _app(tmp_path, rig, users, team)
        app_principal = issue_principal(rig.open(), scopes=all_scopes((WILDCARD,)))

        resp = _client(built).post(path, headers=app_principal.bearer_headers())

        assert resp.status_code == 403, resp.text
        assert resp.json() == {"error": "route_not_allowed"}

    def test_signing_route_refused_with_all_scopes(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        app_principal = issue_principal(rig.open(), scopes=all_scopes((WILDCARD,)))
        resp = _client(built).post(
            "/api/workflows/wf-1/request-signature", headers=app_principal.bearer_headers()
        )
        assert resp.status_code == 403

    def test_unknown_api_path_refused_not_404(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        """Deny-by-default runs before routing: an app cannot map the API by 404s."""
        built = _app(tmp_path, rig, users, team)
        app_principal = issue_principal(rig.open(), scopes=all_scopes((WILDCARD,)))
        resp = _client(built).get("/api/no-such-route", headers=app_principal.bearer_headers())
        assert resp.status_code == 403
        assert len(_refusal_rows(built)) == 1

    @pytest.mark.parametrize(
        "path",
        [
            pytest.param(INBOX_OLIVIA + "/", id="trailing-slash"),
            pytest.param(INBOX_OLIVIA + "/extra", id="suffix"),
            pytest.param("/api/x" + INBOX_OLIVIA, id="prefix"),
        ],
    )
    def test_route_patterns_are_anchored(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path, path: str
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        built.add_probe(INBOX_PATTERN)
        built.add_probe(INBOX_PATTERN + "/{rest:path}")
        app_principal = issue_principal(rig.open(), scopes=all_scopes((WILDCARD,)))
        resp = _client(built).post(path, headers=app_principal.bearer_headers())
        assert resp.status_code == 403, resp.text
        assert built.probes == []


# --- scope + agent binding on an agent_from_path row (REQ-558) ------------------------


class TestScopedRow:
    def test_scoped_call_to_bound_agent_reaches_the_route(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})

        resp = _client(built).post(
            INBOX_OLIVIA,
            json={"subject": "s", "body": "b"},
            headers=app_principal.bearer_headers(),
        )

        assert resp.status_code == 200, resp.text
        (seen,) = built.probes
        assert seen["principal_did"] == app_principal.did
        assert seen["principal_kind"] == "app"
        assert seen["role"] == "app"
        assert _refusal_rows(built) == []

    def test_real_inbox_handler_is_not_refused_by_the_middleware(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        """Without the probe: whatever the handler answers, it is not the middleware's 401/403."""
        built = _app(tmp_path, rig, users, team)
        app_principal = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        resp = _client(built).post(
            INBOX_OLIVIA,
            json={"subject": "s", "body": "b"},
            headers=app_principal.bearer_headers(),
        )
        assert resp.status_code != 401, resp.text
        body = (
            resp.json()
            if resp.headers.get("content-type", "").startswith("application/json")
            else {}
        )
        assert body.get("error") not in {"scope_denied", "invalid_principal", "route_not_allowed"}

    def test_unbound_agent_refused_naming_scope_and_agent(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})

        resp = _client(built).post(INBOX_BOB, json={}, headers=app_principal.bearer_headers())

        assert resp.status_code == 403
        assert resp.json() == {"error": "scope_denied", "scope": "inbox:send", "agent": BOB}
        assert built.probes == []
        (row,) = _refusal_rows(built)
        assert "inbox:send" in row.detail or "inbox:send" in row.operation

    def test_other_scope_for_the_same_agent_refused(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(rig.open(), scopes={Scope.RUNS_READ: (OLIVIA,)})

        resp = _client(built).post(INBOX_OLIVIA, json={}, headers=app_principal.bearer_headers())

        assert resp.status_code == 403
        assert resp.json()["scope"] == "inbox:send"
        assert built.probes == []
        assert len(_refusal_rows(built)) == 1

    def test_agent_label_not_in_roster_refused(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        """An unresolvable agent fails closed, even for a '*' binding."""
        built = _app(tmp_path, rig, users, team)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (WILDCARD,)})
        resp = _client(built).post(
            "/api/agents/ghost/inbox", json={}, headers=app_principal.bearer_headers()
        )
        assert resp.status_code in (403, 404)
        assert built.probes == []

    def test_refusal_row_names_the_app(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        """REQ-566: a refused app is recorded as itself, never as an anonymous session."""
        built = _app(tmp_path, rig, users, team)
        app_principal = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        _client(built).post(INBOX_BOB, json={}, headers=app_principal.bearer_headers())
        (row,) = _refusal_rows(built)
        assert row.actor_did == app_principal.did


# --- credential handling (REQ-555, REQ-559) --------------------------------------------


class TestCredentials:
    def test_malformed_principal_bearer_is_401_not_a_static_token_miss(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        built.add_probe(INBOX_PATTERN)
        resp = _client(built).post(
            INBOX_OLIVIA, json={}, headers={"Authorization": "Bearer arcp_garbage"}
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_principal"}
        assert built.probes == []
        assert len(_refusal_rows(built)) == 1

    def test_wrong_secret_is_401_with_one_audit_row(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        other = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        forged = app_principal.bearer.split(".")[0] + "." + other.bearer.split(".")[1]
        resp = _client(built).post(
            INBOX_OLIVIA, json={}, headers={"Authorization": f"Bearer {forged}"}
        )
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_principal"}
        assert built.probes == []
        assert len(_refusal_rows(built)) == 1

    def test_revoked_principal_is_401_on_the_next_request(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        client = _client(built)
        assert (
            client.post(INBOX_OLIVIA, json={}, headers=app_principal.bearer_headers()).status_code
            == 200
        )

        rig.open().revoke(app_principal.record.id, "did:arc:test:user/revoker")
        resp = client.post(INBOX_OLIVIA, json={}, headers=app_principal.bearer_headers())

        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_principal"}
        assert len(built.probes) == 1
        assert len(_refusal_rows(built)) == 1

    def test_federal_bearer_only_is_401(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team, tier="federal")
        built.add_probe(INBOX_PATTERN)
        _, person = built.sign_in("ada@example.com", (OPERATOR,))
        app_principal = issue_principal(
            rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}, subjects=(person,)
        )
        resp = _client(built).post(INBOX_OLIVIA, json={}, headers=app_principal.bearer_headers())
        assert resp.status_code == 401
        assert resp.json() == {"error": "invalid_principal"}
        assert built.probes == []
        assert len(_refusal_rows(built)) == 1

    def test_federal_signed_request_reaches_the_route(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team, tier="federal")
        built.add_probe(INBOX_PATTERN)
        _, person = built.sign_in("ada@example.com", (OPERATOR,))
        app_principal = issue_principal(
            rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}, subjects=(person,)
        )
        body = b'{"subject":"s","body":"b"}'
        headers = signed_headers(app_principal, method="POST", path=INBOX_OLIVIA, body=body)
        resp = _client(built).post(INBOX_OLIVIA, content=body, headers=headers)
        assert resp.status_code == 200, resp.text
        assert built.probes[-1]["principal_did"] == app_principal.did

    def test_enterprise_requiring_signed_requests_refuses_bearer_only(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team, tier="enterprise", require_signed_requests=True)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(
            rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}, subjects=("did:arc:t:user/x",)
        )
        client = _client(built)

        bearer = client.post(INBOX_OLIVIA, json={}, headers=app_principal.bearer_headers())
        body = b"{}"
        signed = client.post(
            INBOX_OLIVIA,
            content=body,
            headers=signed_headers(app_principal, method="POST", path=INBOX_OLIVIA, body=body),
        )

        assert bearer.status_code == 401
        assert signed.status_code == 200, signed.text
        assert len(built.probes) == 1

    def test_enterprise_default_accepts_bearer(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        """Narrowness: signed mode is not forced at enterprise unless configured."""
        built = _app(tmp_path, rig, users, team, tier="enterprise")
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(
            rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}, subjects=("did:arc:t:user/x",)
        )
        resp = _client(built).post(INBOX_OLIVIA, json={}, headers=app_principal.bearer_headers())
        assert resp.status_code == 200, resp.text

    def test_signed_request_body_still_readable_by_the_route(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team, tier="enterprise", require_signed_requests=True)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(
            rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}, subjects=("did:arc:t:user/x",)
        )
        body = json.dumps({"subject": "quarterly", "body": "x" * 4096}).encode()
        headers = signed_headers(app_principal, method="POST", path=INBOX_OLIVIA, body=body)
        headers["Content-Type"] = "application/json"

        resp = _client(built).post(INBOX_OLIVIA, content=body, headers=headers)

        assert resp.status_code == 200, resp.text
        assert built.probes[-1]["body"] == body.decode()

    def test_signed_request_with_swapped_body_is_401(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team, tier="enterprise", require_signed_requests=True)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(
            rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}, subjects=("did:arc:t:user/x",)
        )
        headers = signed_headers(app_principal, method="POST", path=INBOX_OLIVIA, body=b'{"a":1}')
        resp = _client(built).post(INBOX_OLIVIA, content=b'{"a":2}', headers=headers)
        assert resp.status_code == 401
        assert built.probes == []
        assert len(_refusal_rows(built)) == 1

    def test_replayed_signed_request_is_401(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team, tier="enterprise", require_signed_requests=True)
        built.add_probe(INBOX_PATTERN)
        app_principal = issue_principal(
            rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}, subjects=("did:arc:t:user/x",)
        )
        body = b"{}"
        headers = signed_headers(app_principal, method="POST", path=INBOX_OLIVIA, body=body)
        client = _client(built)
        assert client.post(INBOX_OLIVIA, content=body, headers=headers).status_code == 200
        replay = client.post(INBOX_OLIVIA, content=body, headers=headers)
        assert replay.status_code == 401
        assert len(built.probes) == 1


# --- authority unavailable (REQ-572) -----------------------------------------------------


class TestAuthorityUnavailable:
    def test_factory_raising_is_503_for_apps_while_sessions_still_work(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        app_principal = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})

        def broken() -> Any:
            raise RuntimeError("vault sealed")

        built = build_principal_app(
            tmp_path, principal_store_factory=broken, users=users, team_root=team, tier="personal"
        )
        built.add_probe(INBOX_PATTERN)
        client = _client(built)

        refused = client.post(INBOX_OLIVIA, json={}, headers=app_principal.bearer_headers())
        token, did = built.sign_in("boss@example.com", (OPERATOR,))
        session = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
        op_token = client.post(
            INBOX_OLIVIA, json={}, headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"}
        )

        assert refused.status_code == 503
        assert refused.json() == {"error": "account_authority_unavailable"}
        assert session.status_code == 200 and session.json()["did"] == did
        assert op_token.status_code == 200
        assert len(built.probes) == 1  # only the operator-token call got through

    def test_no_factory_is_503_never_a_fallback(
        self, tmp_path: Path, users: Any, team: Path
    ) -> None:
        built = build_principal_app(
            tmp_path, principal_store_factory=None, users=users, team_root=team, tier="personal"
        )
        built.add_probe(INBOX_PATTERN)
        resp = _client(built).post(
            INBOX_OLIVIA, json={}, headers={"Authorization": "Bearer arcp_" + "0" * 32 + ".x"}
        )
        assert resp.status_code == 503
        assert built.probes == []


# --- require_scope matrix -----------------------------------------------------------------


async def _scope_probe(request: Request) -> JSONResponse:
    from arcui.principals import require_scope

    payload = await request.json()
    refused = require_scope(
        request,
        Scope(payload["scope"]),
        agent_did=payload.get("agent"),
        target="probe:scope",
        operation="probe.scope",
    )
    return refused or JSONResponse({"allowed": True})


def _matrix(built: PrincipalApp, headers: dict[str, str], agent: str | None) -> dict[str, int]:
    """Call require_scope for every scope; record status and audit rows per refusal."""
    client = _client(built)
    outcome: dict[str, int] = {}
    for scope in Scope:
        before = len(_refusal_rows(built))
        resp = client.post(
            SCOPE_PROBE_PATH, json={"scope": scope.value, "agent": agent}, headers=headers
        )
        rows = len(_refusal_rows(built)) - before
        if resp.status_code == 403:
            assert resp.json() == {"error": "scope_denied", "scope": scope.value, "agent": agent}
            assert rows == 1, (scope, rows)
        else:
            assert rows == 0, (scope, rows)
        outcome[scope.value] = resp.status_code
    return outcome


_ALL_200 = {scope.value: 200 for scope in Scope}
_READ_ONLY = {
    scope.value: (200 if scope in {Scope.RUNS_READ, Scope.EVENTS_SUBSCRIBE} else 403)
    for scope in Scope
}


class TestRequireScope:
    @pytest.fixture
    def built(self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path) -> PrincipalApp:
        built = _app(tmp_path, rig, users, team)
        built.add_probe(SCOPE_PROBE, handler=_scope_probe)
        return built

    def test_read_scopes_are_exactly_runs_read_and_events_subscribe(self) -> None:
        from arctrust.principals import READ_SCOPES

        assert {s.value for s in READ_SCOPES} == {"runs:read", "events:subscribe"}

    def test_operator_session_passes_every_scope(self, built: PrincipalApp) -> None:
        token, _ = built.sign_in("boss@example.com", (OPERATOR,))
        assert _matrix(built, {"Authorization": f"Bearer {token}"}, OLIVIA) == _ALL_200

    def test_operator_token_passes_every_scope(self, built: PrincipalApp) -> None:
        assert _matrix(built, {"Authorization": f"Bearer {OPERATOR_TOKEN}"}, OLIVIA) == _ALL_200

    def test_viewer_session_passes_only_read_scopes(self, built: PrincipalApp) -> None:
        token, _ = built.sign_in("watcher@example.com", ())
        assert _matrix(built, {"Authorization": f"Bearer {token}"}, OLIVIA) == _READ_ONLY

    def test_viewer_token_passes_only_read_scopes(self, built: PrincipalApp) -> None:
        assert _matrix(built, {"Authorization": f"Bearer {VIEWER_TOKEN}"}, OLIVIA) == _READ_ONLY

    def test_app_passes_only_its_scope_for_its_bound_agent(
        self, built: PrincipalApp, rig: PrincipalRig
    ) -> None:
        app_principal = issue_principal(rig.open(), scopes={Scope.TASKS_WRITE: (OLIVIA,)})
        headers = app_principal.bearer_headers()
        expected = {s.value: (200 if s is Scope.TASKS_WRITE else 403) for s in Scope}
        assert _matrix(built, headers, OLIVIA) == expected
        assert _matrix(built, headers, BOB) == {s.value: 403 for s in Scope}

    def test_app_with_no_agent_is_refused_even_for_its_own_scope(
        self, built: PrincipalApp, rig: PrincipalRig
    ) -> None:
        app_principal = issue_principal(rig.open(), scopes={Scope.TASKS_WRITE: (WILDCARD,)})
        assert _matrix(built, app_principal.bearer_headers(), None) == {
            s.value: 403 for s in Scope
        }

    def test_app_wildcard_binding_covers_any_agent_at_personal(
        self, built: PrincipalApp, rig: PrincipalRig
    ) -> None:
        app_principal = issue_principal(rig.open(), scopes={Scope.TASKS_WRITE: (WILDCARD,)})
        outcome = _matrix(built, app_principal.bearer_headers(), BOB)
        assert outcome[Scope.TASKS_WRITE.value] == 200
