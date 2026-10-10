"""SPEC-087 COMP-007 / REQ-566: every arcui mutation row names the actor DID.

``MutationAuditFields`` gains a required ``actor_did`` and an ``on_behalf_of``
field, and ``emit_mutation_audit`` fills them from the auth layer's own
request state: a signed-in person is recorded by their account DID; the static
operator token names nobody, so it is recorded by the session pseudo-DID that
``ui_session_actor`` mints. Requests go through the real ``AuthMiddleware`` so
``request.state`` is populated exactly as in production.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arctrust.users import OPERATOR
from packages.arcui.tests.user_authority import user_store_factory
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.audit import MutationAuditFields, UIAuditLogger, emit_mutation_audit
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker

PASSWORD = "correct-horse-battery"
OP_TOKEN = "o" * 64
VIEW_TOKEN = "v" * 64

_BASE_FIELDS: dict[str, Any] = {
    "actor_role": "operator",
    "session_id": "sess-1",
    "target": "task:1",
    "operation": "task.cancel",
    "outcome": "applied",
}


class _SpyWorm:
    def __init__(self) -> None:
        self.written: list[Any] = []

    def write(self, fields: Any) -> None:
        self.written.append(fields)


def _mutating_route(request: Request) -> JSONResponse:
    emit_mutation_audit(request, target="task:1", operation="task.cancel", outcome="applied")
    return JSONResponse({"ok": True})


def _silent_refusal(request: Request) -> JSONResponse:
    return JSONResponse({"error": "Operator role required"}, status_code=403)


class TestMutationAuditFieldsModel:
    def test_missing_actor_did_is_a_validation_error(self) -> None:
        with pytest.raises(ValidationError) as excinfo:
            MutationAuditFields(**_BASE_FIELDS, on_behalf_of=None)
        assert any(err["loc"] == ("actor_did",) for err in excinfo.value.errors())

    def test_actor_did_and_on_behalf_of_are_recorded(self) -> None:
        fields = MutationAuditFields(
            **_BASE_FIELDS,
            actor_did="did:arc:t:user/alice",
            on_behalf_of="did:arc:t:user/bob",
        )
        dumped = fields.model_dump()
        assert dumped["actor_did"] == "did:arc:t:user/alice"
        assert dumped["on_behalf_of"] == "did:arc:t:user/bob"

    def test_on_behalf_of_accepts_none(self) -> None:
        fields = MutationAuditFields(
            **_BASE_FIELDS, actor_did="did:arc:t:user/alice", on_behalf_of=None
        )
        assert fields.on_behalf_of is None


@pytest.fixture
def users_factory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def auth() -> AuthConfig:
    return AuthConfig({"operator_token": OP_TOKEN, "viewer_token": VIEW_TOKEN})


@pytest.fixture
def worm() -> _SpyWorm:
    return _SpyWorm()


@pytest.fixture
def client(auth: AuthConfig, users_factory: Any, worm: _SpyWorm) -> TestClient:
    app = Starlette(
        routes=[
            Route("/api/x/mutate", _mutating_route, methods=["POST"]),
            Route("/api/x/silent", _silent_refusal, methods=["POST"]),
        ]
    )
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.user_store_factory = users_factory
    app.state.session_tracker = SessionTracker()
    app.state.audit = UIAuditLogger()
    app.state.audit_worm = worm
    return TestClient(app)


def _sign_in(
    auth: AuthConfig, users_factory: Any, email: str, roles: tuple[str, ...]
) -> tuple[str, str]:
    """Create an account and a live session for it; return (token, account DID)."""
    user = users_factory().add(email, PASSWORD, roles=roles)
    role = "operator" if user.is_operator else "viewer"
    session = auth.sessions.issue(email=user.email, did=user.did, role=role)
    return session.token, user.did


def _logged_mutations(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    return [
        json.loads(r.message)["details"]
        for r in caplog.records
        if r.name == "arcui.audit" and '"ui.mutation"' in r.message
    ]


class TestEmitMutationAuditActor:
    def test_signed_in_person_is_recorded_by_account_did(
        self,
        client: TestClient,
        auth: AuthConfig,
        users_factory: Any,
        worm: _SpyWorm,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        token, did = _sign_in(auth, users_factory, "boss@example.com", (OPERATOR,))
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post("/api/x/mutate", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 200, resp.text

        (fields,) = worm.written
        assert fields.actor_did == did
        assert fields.on_behalf_of is None
        (logged,) = _logged_mutations(caplog)
        assert logged["actor_did"] == did
        assert logged["on_behalf_of"] is None

    def test_static_operator_token_is_recorded_by_session_pseudo_did(
        self, client: TestClient, worm: _SpyWorm, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post("/api/x/mutate", headers={"Authorization": f"Bearer {OP_TOKEN}"})
        assert resp.status_code == 200, resp.text

        (fields,) = worm.written
        # ui_session_actor: no account DID -> did:arc:ui:session:<session id>.
        assert fields.session_id not in ("", "unknown")
        assert fields.actor_did == f"did:arc:ui:session:{fields.session_id}"
        assert fields.on_behalf_of is None
        (logged,) = _logged_mutations(caplog)
        assert logged["actor_did"] == fields.actor_did

    def test_two_people_are_never_collapsed_into_one_actor(
        self, client: TestClient, auth: AuthConfig, users_factory: Any, worm: _SpyWorm
    ) -> None:
        token_a, did_a = _sign_in(auth, users_factory, "a@example.com", (OPERATOR,))
        token_b, did_b = _sign_in(auth, users_factory, "b@example.com", (OPERATOR,))
        client.post("/api/x/mutate", headers={"Authorization": f"Bearer {token_a}"})
        client.post("/api/x/mutate", headers={"Authorization": f"Bearer {token_b}"})
        assert [f.actor_did for f in worm.written] == [did_a, did_b]
        assert did_a != did_b

    def test_a_refusal_row_names_the_refused_person(
        self, client: TestClient, auth: AuthConfig, users_factory: Any, worm: _SpyWorm
    ) -> None:
        # The auth layer's generic refusal row goes through emit_mutation_audit
        # too, so a viewer's refused change must name the viewer, not "unknown".
        token, did = _sign_in(auth, users_factory, "watcher@example.com", ())
        resp = client.post("/api/x/silent", headers={"Authorization": f"Bearer {token}"})
        assert resp.status_code == 403

        (fields,) = worm.written
        assert fields.outcome == "denied"
        assert fields.actor_did == did
        assert fields.on_behalf_of is None
