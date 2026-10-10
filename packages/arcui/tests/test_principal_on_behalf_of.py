"""SPEC-087 T-1278: on-behalf-of resolution, subject allowlist, causal binding.

REQ-561 (an app may act for a person only if that person is a live, enabled
account on its subject allowlist) and REQ-565 (every act names app AND person:
the causal root is ``initiator='app'``, ``initiator_id`` = app DID,
``on_behalf_of`` = person DID).

Real ``create_app`` + real ``AuthMiddleware`` + real ``UserStore`` and
``PrincipalStore`` (``principal_fixtures``). A probe route mounted ahead of the
real inbox handler reports what the middleware bound, including the causal
context stamped onto an ``AuditEvent`` constructed inside the route.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust.principals import Scope
from arctrust.users import OPERATOR
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    WILDCARD,
    PrincipalApp,
    PrincipalRig,
    agent_did,
    build_principal_app,
    issue_principal,
    principal_rig,
    signed_headers,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

OLIVIA = agent_did()
INBOX = "/api/agents/olivia/inbox"
INBOX_PATTERN = "/api/agents/{id}/inbox"
OBO = "X-Arc-On-Behalf-Of"
IGNORED_OP = "principal.on_behalf_of.ignored"


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def rig(tmp_path: Path) -> PrincipalRig:
    return principal_rig(tmp_path / "authority")


@pytest.fixture
def team(tmp_path: Path) -> Path:
    return write_team(tmp_path / "team", {"olivia": OLIVIA})


@pytest.fixture
def built(tmp_path: Path, rig: PrincipalRig, users: Any, team: Path) -> PrincipalApp:
    built = build_principal_app(
        tmp_path,
        principal_store_factory=rig.factory,
        users=users,
        team_root=team,
        tier="personal",
    )
    built.add_probe(INBOX_PATTERN)
    return built


@pytest.fixture
def people(built: PrincipalApp) -> dict[str, str]:
    """ada and bob are enabled operators; carol exists but is disabled."""
    _, ada = built.sign_in("ada@example.com", (OPERATOR,))
    _, bob = built.sign_in("bob@example.com", (OPERATOR,))
    _, carol = built.sign_in("carol@example.com", (OPERATOR,))
    built.users().set_disabled("carol@example.com", True)
    return {"ada": ada, "bob": bob, "carol": carol}


def _client(built: PrincipalApp) -> TestClient:
    return TestClient(built.app, raise_server_exceptions=False)


def _app_for(rig: PrincipalRig, subjects: tuple[str, ...]) -> Any:
    return issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)}, subjects=subjects)


class TestResolution:
    def test_header_by_did_resolves_the_person(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        app = _app_for(rig, (people["ada"],))
        resp = _client(built).post(
            INBOX, json={}, headers=app.bearer_headers(**{OBO: people["ada"]})
        )
        assert resp.status_code == 200, resp.text
        (seen,) = built.probes
        assert seen["principal_on_behalf_of"] == people["ada"]
        assert seen["state_on_behalf_of"] == people["ada"]

    def test_header_by_email_resolves_to_the_person_did(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        app = _app_for(rig, (people["ada"],))
        resp = _client(built).post(
            INBOX, json={}, headers=app.bearer_headers(**{OBO: "ada@example.com"})
        )
        assert resp.status_code == 200, resp.text
        assert built.probes[-1]["principal_on_behalf_of"] == people["ada"]

    def test_no_header_means_no_person(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        app = _app_for(rig, (people["ada"],))
        assert _client(built).post(INBOX, json={}, headers=app.bearer_headers()).status_code == 200
        assert built.probes[-1]["principal_on_behalf_of"] is None

    def test_wildcard_subjects_admit_any_enabled_person_at_personal(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        app = _app_for(rig, (WILDCARD,))
        resp = _client(built).post(
            INBOX, json={}, headers=app.bearer_headers(**{OBO: people["bob"]})
        )
        assert resp.status_code == 200, resp.text
        assert built.probes[-1]["principal_on_behalf_of"] == people["bob"]

    def test_signed_request_on_behalf_of_resolves(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = build_principal_app(
            tmp_path,
            principal_store_factory=rig.factory,
            users=users,
            team_root=team,
            tier="enterprise",
            require_signed_requests=True,
        )
        built.add_probe(INBOX_PATTERN)
        _, ada = built.sign_in("ada@example.com", (OPERATOR,))
        app = _app_for(rig, (ada,))
        body = b"{}"
        headers = signed_headers(app, method="POST", path=INBOX, body=body, on_behalf_of=ada)
        resp = _client(built).post(INBOX, content=body, headers=headers)
        assert resp.status_code == 200, resp.text
        assert built.probes[-1]["principal_on_behalf_of"] == ada


class TestRefusal:
    @pytest.mark.parametrize(
        ("who", "subjects_key"),
        [
            pytest.param("did:arc:default:user/nobody00", "ada", id="unknown-did"),
            pytest.param("nobody@example.com", "ada", id="unknown-email"),
            pytest.param("carol", "carol", id="disabled"),
            pytest.param("carol@example.com", "carol", id="disabled-by-email"),
            pytest.param("bob", "ada", id="not-allowlisted"),
            pytest.param("bob@example.com", "ada", id="not-allowlisted-by-email"),
            pytest.param("x" * 5000, "ada", id="oversized"),
        ],
    )
    def test_refused_person_is_403_with_one_audit_row(
        self,
        built: PrincipalApp,
        rig: PrincipalRig,
        people: dict[str, str],
        who: str,
        subjects_key: str,
    ) -> None:
        app = _app_for(rig, (people[subjects_key],))
        header = people.get(who, who)
        before = len(built.worm.denied())

        resp = _client(built).post(INBOX, json={}, headers=app.bearer_headers(**{OBO: header}))

        assert resp.status_code == 403, resp.text
        assert built.probes == []
        assert len(built.worm.denied()) - before == 1

    def test_disabled_person_refused_even_under_wildcard_subjects(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        app = _app_for(rig, (WILDCARD,))
        resp = _client(built).post(
            INBOX, json={}, headers=app.bearer_headers(**{OBO: people["carol"]})
        )
        assert resp.status_code == 403
        assert built.probes == []
        assert len(built.worm.denied()) == 1

    def test_refusals_do_not_reveal_which_people_exist(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        """Unknown, disabled and not-allowlisted look identical to the app (no enumeration)."""
        app = _app_for(rig, (people["ada"],))
        client = _client(built)
        bodies = {
            client.post(INBOX, json={}, headers=app.bearer_headers(**{OBO: who})).text
            for who in ("nobody@example.com", people["carol"], people["bob"])
        }
        assert len(bodies) == 1

    def test_person_disabled_after_issuance_refused_on_next_call(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        app = _app_for(rig, (people["ada"],))
        client = _client(built)
        headers = app.bearer_headers(**{OBO: people["ada"]})
        assert client.post(INBOX, json={}, headers=headers).status_code == 200
        built.users().set_disabled("ada@example.com", True)
        assert client.post(INBOX, json={}, headers=headers).status_code == 403
        assert len(built.probes) == 1


class TestNonAppCallersIgnoreTheHeader:
    def test_session_request_with_header_is_served_and_header_ignored(
        self, built: PrincipalApp, people: dict[str, str]
    ) -> None:
        token, did = built.sign_in("boss@example.com", (OPERATOR,))
        resp = _client(built).post(
            INBOX,
            json={},
            headers={"Authorization": f"Bearer {token}", OBO: people["ada"]},
        )
        assert resp.status_code == 200, resp.text
        (seen,) = built.probes
        assert seen["principal_kind"] == "user"
        assert seen["principal_did"] == did
        assert seen["principal_on_behalf_of"] is None
        assert seen["state_on_behalf_of"] is None
        assert seen["causal"]["on_behalf_of"] is None

        (row,) = built.worm.written
        assert row.operation == IGNORED_OP
        assert row.outcome == "denied"

    def test_static_token_request_with_header_is_served_and_header_ignored(
        self, built: PrincipalApp, people: dict[str, str]
    ) -> None:
        resp = _client(built).post(
            INBOX,
            json={},
            headers={"Authorization": f"Bearer {OPERATOR_TOKEN}", OBO: people["ada"]},
        )
        assert resp.status_code == 200, resp.text
        (seen,) = built.probes
        assert seen["principal_kind"] == "operator"
        assert seen["state_on_behalf_of"] is None
        assert [row.operation for row in built.worm.written] == [IGNORED_OP]

    def test_session_without_header_writes_no_anomaly_row(
        self, built: PrincipalApp, people: dict[str, str]
    ) -> None:
        token, _ = built.sign_in("boss@example.com", (OPERATOR,))
        resp = _client(built).post(INBOX, json={}, headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200
        assert built.worm.written == []


class TestCausalBinding:
    def test_event_inside_route_names_app_and_person(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        app = _app_for(rig, (people["ada"],))
        resp = _client(built).post(
            INBOX, json={}, headers=app.bearer_headers(**{OBO: "ada@example.com"})
        )
        assert resp.status_code == 200, resp.text
        causal = built.probes[-1]["causal"]
        assert causal["initiator"] == "app"
        assert causal["initiator_id"] == app.did
        assert causal["on_behalf_of"] == people["ada"]

    def test_event_inside_route_names_app_alone_without_header(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        app = _app_for(rig, (people["ada"],))
        assert _client(built).post(INBOX, json={}, headers=app.bearer_headers()).status_code == 200
        causal = built.probes[-1]["causal"]
        assert causal["initiator"] == "app"
        assert causal["initiator_id"] == app.did
        assert causal["on_behalf_of"] is None

    def test_causal_binding_does_not_leak_between_requests(
        self, built: PrincipalApp, rig: PrincipalRig, people: dict[str, str]
    ) -> None:
        app = _app_for(rig, (people["ada"],))
        client = _client(built)
        client.post(INBOX, json={}, headers=app.bearer_headers(**{OBO: people["ada"]}))
        client.post(INBOX, json={}, headers={"Authorization": f"Bearer {OPERATOR_TOKEN}"})
        after = built.probes[-1]["causal"]
        assert after["initiator"] == "ui_session"
        assert after["on_behalf_of"] is None
