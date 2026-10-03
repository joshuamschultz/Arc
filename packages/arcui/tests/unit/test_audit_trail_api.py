"""Item 20 P20-5 — the audit API: reads leave no WORM row, filters, run timeline, reverify."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from arcstore.backends.memory import FakeBackend
from arctrust import OperatorKey, causal, default_operator_key_path
from arctrust.audit import AuditEvent, NullSink, WormSink
from arctrust.keypair import generate_keypair
from arctrust.signer import InProcessSigner
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import build_mutation_worm_writer, operator_audit_sink
from arcui.auth import AuthConfig, AuthMiddleware, SessionTracker
from arcui.observe import Observe
from arcui.routes.observe_run import routes as run_routes
from arcui.routes.team_pages import routes as team_routes
from arcui.routes.workflows import routes as workflow_routes

_AUTH = {"viewer_token": "viewer", "operator_token": "operator"}


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed(worm: Path) -> bytes:
    """Three agent events with distinct causal chains, signed by one key."""
    kp = generate_keypair()
    sink = WormSink(worm / "audit-chain-olivia.jsonl", InProcessSigner(kp.private_key))
    plans = [
        ("run-1", {"tool_call_id": "tc-1"}),
        ("run-1", {"llm_call_id": "llm-1"}),
        ("run-2", {"workflow_run_id": "wf-1", "node_id": "n1", "connection_id": "conn-1"}),
    ]
    for run_id, extra in plans:
        with (
            causal.bind(causal.root("agent", "did:arc:t:olivia/1")),
            causal.refine(run_id=run_id, **extra),
        ):
            sink.write(
                AuditEvent(
                    actor_did="did:arc:t:olivia/1",
                    action="policy.evaluate",
                    target="t",
                    outcome="allow",
                )
            )
    sink.close()
    return kp.public_key


@pytest.fixture
async def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc"))
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    worm = tmp_path / "data" / "worm"
    worm.mkdir(parents=True)
    key = _seed(worm)
    writer = build_mutation_worm_writer(tmp_path / "data")
    observe = Observe(data_dir=tmp_path / "data", backend=FakeBackend(), worm_public_key=key)
    await observe.refresh()
    auth = AuthConfig(_AUTH)
    app = Starlette(routes=[*team_routes, *run_routes, *workflow_routes])
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.session_tracker = SessionTracker()
    app.state.observe = observe
    app.state.audit_worm = writer
    app.state.roster_provider = lambda: []
    return TestClient(app)


def _chain_actions(tmp_path: Path) -> list[str]:
    chain = tmp_path / "data" / "worm" / "audit-chain-arcui.jsonl"
    return [json.loads(x)["event"]["action"] for x in chain.read_text().splitlines() if x]


class TestReadsLeaveNoChainRow:
    def test_operator_audit_sink_discards_for_a_get(self) -> None:
        class _State:
            audit_worm = type("W", (), {"sink": object()})()

        class _App:
            state = _State()

        class _Req:
            method = "GET"
            app = _App()

        assert isinstance(operator_audit_sink(_Req()), NullSink)

    def test_workflow_list_writes_no_chain_row(self, client: TestClient, tmp_path: Path) -> None:
        class _Plane:
            async def list_workflows(self, **_: Any) -> list[dict[str, Any]]:
                return []

        client.app.state.workflow_control_plane = _Plane()  # type: ignore[attr-defined]
        assert client.get("/api/workflows", headers=_bearer("operator")).status_code == 200
        client.app.state.audit_worm.sink.close()  # type: ignore[attr-defined]
        assert "workflow.list" not in _chain_actions(tmp_path)


class TestAuditFilters:
    @pytest.mark.parametrize(
        ("query", "expected"),
        [
            ("run_id=run-1", 2),
            ("initiator=agent", 3),
            ("initiator=scheduler", 0),
            ("tool_call_id=tc-1", 1),
            ("workflow_run_id=wf-1", 1),
            ("connection_id=conn-1", 1),
            ("agent=did:arc:t:olivia/1", 3),
            ("agent=did:arc:other", 0),
        ],
    )
    def test_filter(self, client: TestClient, query: str, expected: int) -> None:
        resp = client.get(f"/api/team/audit?{query}", headers=_bearer("viewer"))
        assert resp.status_code == 200
        assert len(resp.json()["events"]) == expected

    def test_a_malformed_filter_value_is_refused(self, client: TestClient) -> None:
        resp = client.get("/api/team/audit?run_id=a b;drop", headers=_bearer("viewer"))
        assert resp.status_code == 400


class TestRunAudit:
    def test_run_audit_lists_only_that_runs_events(self, client: TestClient) -> None:
        resp = client.get("/api/runs/run-1/audit", headers=_bearer("viewer"))
        assert resp.status_code == 200
        events = resp.json()["events"]
        assert len(events) == 2
        assert {e["run_id"] for e in events} == {"run-1"}


class TestReverify:
    def test_viewer_cannot_reverify(self, client: TestClient) -> None:
        resp = client.post("/api/team/audit/reverify", headers=_bearer("viewer"))
        assert resp.status_code == 403

    def test_operator_reverify_returns_summary_and_is_audited(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        resp = client.post("/api/team/audit/reverify", headers=_bearer("operator"))
        assert resp.status_code == 200
        assert resp.json()["verified"] == 3
        client.app.state.audit_worm.sink.close()  # type: ignore[attr-defined]
        assert "audit.reverify" in _chain_actions(tmp_path)
