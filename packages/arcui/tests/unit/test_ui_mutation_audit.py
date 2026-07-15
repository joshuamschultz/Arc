"""UI mutation audit helper (COMP-010 / REQ-088, REQ-091, REQ-092).

One emission point wraps the shared ``app.state.audit`` sink so every
UI-originated mutation records actor / target / operation / outcome
uniformly. These tests prove the event lands in the sink with all fields,
and that the auth layer populates the session id the helper attributes to.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arctrust import OperatorKey, default_operator_key_path
from arctrust.audit import verify_chain
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.audit import (
    UIAuditEvent,
    UIAuditLogger,
    build_mutation_worm_writer,
    emit_mutation_audit,
)
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
    def __init__(self, audit: Any, audit_worm: Any = None) -> None:
        self.state = _FakeState(audit=audit, audit_worm=audit_worm)


class _FakeRequest:
    def __init__(
        self,
        *,
        audit: Any,
        role: str | None,
        session_id: str | None,
        audit_worm: Any = None,
    ) -> None:
        self.app = _FakeApp(audit, audit_worm)
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


class _SpyWorm:
    """Captures MutationWormWriter.write calls so a test can assert the record."""

    def __init__(self) -> None:
        self.written: list[Any] = []

    def write(self, fields: Any) -> None:
        self.written.append(fields)


class TestMutationWorm:
    def test_mutation_also_written_to_worm(self) -> None:
        # The log/OTel sink AND the WORM chain are independent surfaces: a mutation
        # reaches both.
        spy_audit, spy_worm = _SpyAudit(), _SpyWorm()
        req = _FakeRequest(
            audit=spy_audit, audit_worm=spy_worm, role="operator", session_id="sess-9"
        )
        emit_mutation_audit(req, target="task:42", operation="task.cancel", outcome="applied")
        assert len(spy_audit.events) == 1
        assert len(spy_worm.written) == 1
        fields = spy_worm.written[0]
        assert fields.operation == "task.cancel"
        assert fields.target == "task:42"
        assert fields.outcome == "applied"

    def test_no_worm_writer_is_noop(self) -> None:
        # Absent WORM writer (uninitialised deployment / bare test app) is tolerated.
        req = _FakeRequest(audit=_SpyAudit(), audit_worm=None, role="operator", session_id="s")
        emit_mutation_audit(req, target="t", operation="op", outcome="applied")


def _init_operator_key(config_dir: Path) -> bytes:
    """Generate an on-box operator key under ``config_dir`` and return its public key."""
    key = OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    return key.public_key


class TestBuildMutationWormWriter:
    def test_emits_verifiable_signed_record(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A real operator key + WormSink: the mutation lands as a signed, chained,
        # verifiable record in the SAME worm dir the Observe ingest tails — the fix
        # for mutations never reaching the Security screen.
        monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
        public_key = _init_operator_key(tmp_path / "arc")
        data_dir = tmp_path / "data"

        writer = build_mutation_worm_writer(data_dir)
        assert writer is not None
        req = _FakeRequest(
            audit=None, audit_worm=writer, role="operator", session_id="sess-1"
        )
        emit_mutation_audit(
            req, target="approval:7", operation="approval.approve", outcome="applied"
        )
        writer.sink.close()

        chain = data_dir / "worm" / "audit-chain-arcui.jsonl"
        assert chain.exists()
        assert verify_chain(chain, public_key) is True
        records = [json.loads(line) for line in chain.read_text().splitlines()]
        events = [r["event"] for r in records]
        # The ingest maps action/target/outcome into the audit_chain the Security
        # screen reads.
        assert any(
            e["action"] == "approval.approve"
            and e["target"] == "approval:7"
            and e["outcome"] == "applied"
            and e["actor_did"] == writer.operator_did
            for e in events
        )

    def test_absent_operator_key_degrades_to_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No operator key on the box → degrade to log+OTel only, never mint one.
        monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "empty"))
        assert build_mutation_worm_writer(tmp_path / "data") is None


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
