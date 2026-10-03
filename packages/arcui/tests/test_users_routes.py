"""People from the browser (J-O1): first operator, invites, roles, disable, reset.

A customer must finish every account step in arcui, with no terminal. The first
operator account is bound to a one-time setup code the server writes to its log,
and is refused once any operator exists. After that, only an operator manages
people, every change is audited, and links work exactly once.
"""

from __future__ import annotations

import logging
import re
from typing import Any

import pytest
from arctrust.users import OPERATOR
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app

PASSWORD = "correct-horse-battery"
OP_TOKEN = "o" * 64
VIEW_TOKEN = "v" * 64
_CODE_RE = re.compile(r"ARC FIRST-RUN SETUP CODE: ([A-Z0-9-]+)")


class AuditRecorder:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, name: str, fields: dict[str, Any]) -> None:
        self.events.append((str(name), fields))

    def operations(self, outcome: str | None = None) -> list[str]:
        return [
            f["operation"]
            for _, f in self.events
            if "operation" in f and (outcome is None or f["outcome"] == outcome)
        ]


@pytest.fixture
def factory(tmp_path, monkeypatch):
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    return user_store_factory(tmp_path / "state")


@pytest.fixture
def audit() -> AuditRecorder:
    return AuditRecorder()


@pytest.fixture
def client(factory, audit):
    app = create_app(
        auth_config=AuthConfig({"viewer_token": VIEW_TOKEN, "operator_token": OP_TOKEN}),
        user_store_factory=factory,
    )
    app.state.audit = audit
    return TestClient(app)


def _op() -> dict[str, str]:
    return {"Authorization": f"Bearer {OP_TOKEN}"}


def _viewer() -> dict[str, str]:
    return {"Authorization": f"Bearer {VIEW_TOKEN}"}


def _setup_code(client: TestClient, caplog: pytest.LogCaptureFixture) -> str:
    with caplog.at_level(logging.WARNING):
        body = client.get("/api/auth/mode").json()
    assert body["setup_available"] is True
    codes = _CODE_RE.findall(caplog.text)
    assert codes, "the setup code must be written to the server log"
    return str(codes[-1])


def _first_operator(client: TestClient, caplog: pytest.LogCaptureFixture) -> dict[str, Any]:
    code = _setup_code(client, caplog)
    resp = client.post(
        "/api/auth/setup",
        json={"setup_code": code, "email": "owner@example.com", "password": PASSWORD},
    )
    assert resp.status_code == 200, resp.text
    return dict(resp.json())


# --- first run ------------------------------------------------------------


def test_a_fresh_install_offers_first_run_setup_and_logs_one_code(client, caplog):
    with caplog.at_level(logging.WARNING):
        first = client.get("/api/auth/mode").json()
        client.get("/api/auth/mode")
    assert first == {"login_available": False, "setup_available": True}
    assert len(set(_CODE_RE.findall(caplog.text))) == 1, "one code per process, not per visit"


def test_first_run_creates_a_signed_in_operator(client, caplog, factory):
    body = _first_operator(client, caplog)

    assert body["role"] == "operator"
    assert body["email"] == "owner@example.com"
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {body['token']}"}).json()
    assert me["anonymous"] is False and me["role"] == "operator"
    assert factory().get("owner@example.com").is_operator
    assert client.get("/api/auth/mode").json() == {
        "login_available": True,
        "setup_available": False,
    }


def test_a_replayed_setup_code_is_refused(client, caplog, factory, audit):
    """Abuse: the setup code is replayed after its first use."""
    code = _setup_code(client, caplog)
    ok = client.post(
        "/api/auth/setup",
        json={"setup_code": code, "email": "owner@example.com", "password": PASSWORD},
    )
    assert ok.status_code == 200

    replay = client.post(
        "/api/auth/setup",
        json={"setup_code": code, "email": "intruder@example.com", "password": PASSWORD},
    )

    assert replay.status_code == 409
    assert factory().get("intruder@example.com") is None
    assert "user.setup" in audit.operations("denied")


def test_setup_is_refused_when_an_operator_already_exists(client, caplog, factory):
    """Abuse: first-run setup attempted on a deployment that already has an operator."""
    factory().add("boss@example.com", PASSWORD, roles=(OPERATOR,))

    with caplog.at_level(logging.WARNING):
        mode = client.get("/api/auth/mode").json()
    resp = client.post(
        "/api/auth/setup",
        json={"setup_code": "ANY-GUESS", "email": "intruder@example.com", "password": PASSWORD},
    )

    assert mode == {"login_available": True, "setup_available": False}
    assert not _CODE_RE.search(caplog.text), "no code is minted once an operator exists"
    assert resp.status_code == 409
    assert factory().get("intruder@example.com") is None


def test_a_wrong_setup_code_is_refused_and_guessing_locks_out(client, caplog, factory):
    code = _setup_code(client, caplog)
    for _ in range(5):
        wrong = client.post(
            "/api/auth/setup",
            json={"setup_code": "WRONG-CODE", "email": "x@example.com", "password": PASSWORD},
        )
        assert wrong.status_code == 401
    locked = client.post(
        "/api/auth/setup",
        json={"setup_code": code, "email": "owner@example.com", "password": PASSWORD},
    )
    assert locked.status_code == 429
    assert factory().is_empty()


def test_a_weak_password_keeps_the_setup_code_usable(client, caplog):
    code = _setup_code(client, caplog)
    weak = client.post(
        "/api/auth/setup",
        json={"setup_code": code, "email": "owner@example.com", "password": "short"},
    )
    assert weak.status_code == 400
    assert "12" in weak.json()["error"]
    ok = client.post(
        "/api/auth/setup",
        json={"setup_code": code, "email": "owner@example.com", "password": PASSWORD},
    )
    assert ok.status_code == 200


def test_the_sign_in_screen_never_names_a_command(client):
    body = client.get("/api/auth/mode").text
    assert "arc " not in body


# --- operator management ----------------------------------------------------


def test_a_viewer_cannot_create_or_list_people(client, factory, audit):
    """Abuse: a viewer tries to create a user."""
    resp = client.post(
        "/api/users",
        json={"email": "new@example.com", "password": PASSWORD, "role": "operator"},
        headers=_viewer(),
    )
    listing = client.get("/api/users", headers=_viewer())

    assert resp.status_code == 403
    assert listing.status_code == 403
    assert factory().get("new@example.com") is None
    assert "user.add" in audit.operations("denied")


def test_an_operator_adds_a_person_and_it_is_audited(client, factory, audit):
    resp = client.post(
        "/api/users",
        json={"email": "Ann@Example.com", "password": PASSWORD, "role": "viewer"},
        headers=_op(),
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["user"]["email"] == "ann@example.com"
    assert "password_hash" not in resp.text

    login = client.post("/api/auth/login", json={"email": "ann@example.com", "password": PASSWORD})
    assert login.json()["role"] == "viewer"
    listed = client.get("/api/users", headers=_op()).json()["users"]
    assert [u["email"] for u in listed] == ["ann@example.com"]
    assert "user.add" in audit.operations("applied")


def test_passwords_are_hashed_the_way_the_cli_hashes_them(client, factory):
    client.post(
        "/api/users",
        json={"email": "ann@example.com", "password": PASSWORD, "role": "viewer"},
        headers=_op(),
    )
    stored = factory().get("ann@example.com")
    assert stored.password_hash.startswith("$argon2id$")
    assert PASSWORD not in (factory().path.read_text(encoding="utf-8"))


def test_an_invite_link_works_once(client, factory):
    """Abuse: an invite link is reused."""
    invite = client.post(
        "/api/users/invites", json={"email": "bob@example.com", "role": "viewer"}, headers=_op()
    )
    assert invite.status_code == 201, invite.text
    token = invite.json()["link_path"].split("invite=", 1)[1]
    check = client.post("/api/auth/invite/check", json={"token": token})
    assert check.json() == {"email": "bob@example.com", "kind": "invite", "role": "viewer"}

    accepted = client.post("/api/auth/invite/accept", json={"token": token, "password": PASSWORD})
    reused = client.post(
        "/api/auth/invite/accept", json={"token": token, "password": "another-long-password"}
    )

    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["role"] == "viewer"
    assert reused.status_code == 404
    assert client.post("/api/auth/invite/check", json={"token": token}).status_code == 404
    assert factory().verify("bob@example.com", PASSWORD) is not None


def test_an_invite_cannot_be_escalated_by_the_person_accepting_it(client, factory):
    """Abuse: a forged role escalation through the accept body."""
    token = (
        client.post(
            "/api/users/invites",
            json={"email": "bob@example.com", "role": "viewer"},
            headers=_op(),
        )
        .json()["link_path"]
        .split("invite=", 1)[1]
    )

    forged = client.post(
        "/api/auth/invite/accept",
        json={"token": token, "password": PASSWORD, "role": "operator"},
    )

    assert forged.status_code == 400
    assert factory().get("bob@example.com") is None


def test_a_viewer_cannot_change_any_role_including_their_own(client, factory):
    """Abuse: a viewer forges a role change, directly or through their profile."""
    factory().add("watcher@example.com", PASSWORD)
    session = client.post(
        "/api/auth/login", json={"email": "watcher@example.com", "password": PASSWORD}
    ).json()["token"]
    auth = {"Authorization": f"Bearer {session}"}

    direct = client.put(
        "/api/users/watcher@example.com/role", json={"role": "operator"}, headers=auth
    )
    profile = client.patch("/api/auth/me", json={"roles": ["operator"]}, headers=auth)

    assert direct.status_code == 403
    assert profile.status_code == 400
    assert not factory().get("watcher@example.com").is_operator


def test_an_operator_changes_a_role(client, factory, audit):
    factory().add("watcher@example.com", PASSWORD)
    resp = client.put(
        "/api/users/watcher@example.com/role", json={"role": "operator"}, headers=_op()
    )
    assert resp.status_code == 200, resp.text
    assert factory().get("watcher@example.com").is_operator
    assert "user.role" in audit.operations("applied")


def test_an_unknown_role_is_refused(client, factory):
    factory().add("watcher@example.com", PASSWORD)
    resp = client.put("/api/users/watcher@example.com/role", json={"role": "root"}, headers=_op())
    assert resp.status_code == 400


def test_disable_signs_the_person_out_and_blocks_sign_in(client, factory):
    factory().add("watcher@example.com", PASSWORD)
    session = client.post(
        "/api/auth/login", json={"email": "watcher@example.com", "password": PASSWORD}
    ).json()["token"]

    resp = client.post("/api/users/watcher@example.com/disable", headers=_op())

    assert resp.status_code == 200, resp.text
    assert resp.json()["user"]["disabled"] is True
    me = client.get("/api/auth/me", headers={"Authorization": f"Bearer {session}"})
    assert me.status_code == 401
    login = client.post(
        "/api/auth/login", json={"email": "watcher@example.com", "password": PASSWORD}
    )
    assert login.status_code == 401
    assert client.post("/api/users/watcher@example.com/enable", headers=_op()).status_code == 200
    assert (
        client.post(
            "/api/auth/login", json={"email": "watcher@example.com", "password": PASSWORD}
        ).status_code
        == 200
    )


def test_the_last_operator_cannot_be_demoted_or_disabled(client, factory):
    factory().add("boss@example.com", PASSWORD, roles=(OPERATOR,))

    demote = client.put("/api/users/boss@example.com/role", json={"role": "viewer"}, headers=_op())
    disable = client.post("/api/users/boss@example.com/disable", headers=_op())

    assert demote.status_code == 409
    assert disable.status_code == 409
    assert factory().get("boss@example.com").is_operator


def test_a_reset_link_sets_a_new_password_once(client, factory):
    factory().add("watcher@example.com", PASSWORD)
    link = client.post("/api/users/watcher@example.com/reset-link", headers=_op())
    assert link.status_code == 201, link.text
    token = link.json()["link_path"].split("invite=", 1)[1]
    assert client.post("/api/auth/invite/check", json={"token": token}).json()["kind"] == "reset"

    new_password = "a-brand-new-long-password"
    done = client.post("/api/auth/invite/accept", json={"token": token, "password": new_password})
    again = client.post("/api/auth/invite/accept", json={"token": token, "password": PASSWORD})

    assert done.status_code == 200, done.text
    assert again.status_code == 404
    assert factory().verify("watcher@example.com", new_password) is not None
    assert factory().verify("watcher@example.com", PASSWORD) is None


def test_a_link_for_one_kind_cannot_be_used_after_the_person_is_removed(client, factory):
    factory().add("watcher@example.com", PASSWORD)
    token = (
        client.post("/api/users/watcher@example.com/reset-link", headers=_op())
        .json()["link_path"]
        .split("invite=", 1)[1]
    )
    factory().remove("watcher@example.com")

    resp = client.post("/api/auth/invite/accept", json={"token": token, "password": PASSWORD})

    assert resp.status_code == 404
    assert factory().get("watcher@example.com") is None
