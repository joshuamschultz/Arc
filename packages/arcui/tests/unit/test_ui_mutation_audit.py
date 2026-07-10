"""UI mutation audit helper (COMP-010 / REQ-088, REQ-091, REQ-092).

One emission point wraps the shared ``app.state.audit`` sink so every
UI-originated mutation records actor / target / operation / outcome
uniformly. These tests prove the event lands in the sink with all fields,
and that the auth layer populates the session id the helper attributes to.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.audit import UIAuditEvent, UIAuditLogger, emit_mutation_audit
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker


class _SpyAudit:
    """Captures audit_event calls so a test can assert the payload."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def audit_event(self, event_type: Any, details: dict[str, Any]) -> None:
        name = event_type.value if isinstance(event_type, UIAuditEvent) else event_type
        self.events.append((name, details))


class _FakeState:
    def __init__(self, **kw: Any) -> None:
        self.__dict__.update(kw)


class _FakeApp:
    def __init__(self, audit: Any) -> None:
        self.state = _FakeState(audit=audit)


class _FakeRequest:
    def __init__(self, *, audit: Any, role: str | None, session_id: str | None) -> None:
        self.app = _FakeApp(audit)
        self.state = _FakeState(role=role, session_id=session_id)


class TestEmitMutationAudit:
    def test_emits_event_with_all_fields(self) -> None:
        spy = _SpyAudit()
        req = _FakeRequest(audit=spy, role="operator", session_id="sess-123")
        emit_mutation_audit(
            req,
            target="channel://work",
            operation="channel.create",
            outcome="applied",
            detail="members=[a,b]",
        )
        assert len(spy.events) == 1
        name, details = spy.events[0]
        assert name == "ui.mutation"
        assert details == {
            "actor_role": "operator",
            "session_id": "sess-123",
            "target": "channel://work",
            "operation": "channel.create",
            "outcome": "applied",
            "detail": "members=[a,b]",
        }

    def test_missing_actor_falls_back_to_unknown(self) -> None:
        spy = _SpyAudit()
        req = _FakeRequest(audit=spy, role=None, session_id=None)
        emit_mutation_audit(req, target="t", operation="op", outcome="denied")
        _, details = spy.events[0]
        assert details["actor_role"] == "unknown"
        assert details["session_id"] == "unknown"

    def test_no_audit_sink_is_noop(self) -> None:
        req = _FakeRequest(audit=None, role="operator", session_id="s")
        # Must not raise when the app has no audit sink.
        emit_mutation_audit(req, target="t", operation="op", outcome="applied")


def _mutating_route(request: Request) -> JSONResponse:
    emit_mutation_audit(
        request,
        target="channel://demo",
        operation="channel.create",
        outcome="applied",
    )
    return JSONResponse({"ok": True})


class TestSessionIdWiring:
    def test_auth_layer_supplies_session_id_to_mutation_audit(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        auth = AuthConfig({"operator_token": "op-tok", "viewer_token": "view-tok"})
        app = Starlette(routes=[Route("/api/x/mutate", _mutating_route, methods=["POST"])])
        app.add_middleware(AuthMiddleware, auth_config=auth)
        app.state.audit = UIAuditLogger()
        app.state.session_tracker = SessionTracker()

        client = TestClient(app)
        with caplog.at_level("INFO", logger="arcui.audit"):
            resp = client.post("/api/x/mutate", headers={"Authorization": "Bearer op-tok"})
        assert resp.status_code == 200

        mutations = [
            json.loads(r.message)
            for r in caplog.records
            if r.name == "arcui.audit" and '"ui.mutation"' in r.message
        ]
        assert len(mutations) == 1
        details = mutations[0]["details"]
        assert details["actor_role"] == "operator"
        # 16-hex-char session id minted by the auth layer (secrets.token_hex(8)).
        assert len(details["session_id"]) == 16
        assert details["session_id"] != "unknown"
