"""Authenticated operator queue controls share the running coordinator."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import arcrun
import pytest
from starlette.applications import Starlette
from starlette.testclient import TestClient

from arcui.audit import UIAuditLogger
from arcui.auth import AuthConfig, AuthMiddleware
from arcui.routes.queue import routes
from arcui.server import create_app


@dataclass(frozen=True)
class _User:
    did: str
    is_operator: bool
    disabled: bool = False


class _Users:
    def __init__(self) -> None:
        self.users = {
            "operator@example.com": _User("did:arc:tenant-a:user/operator", True),
            "viewer@example.com": _User("did:arc:tenant-a:user/viewer", False),
            "foreign@example.com": _User("did:arc:tenant-b:user/foreign", True),
        }

    def get(self, email: str) -> _User | None:
        return self.users.get(email)


class _Worm:
    def write(self, _fields: object) -> None:
        pass


class _FailingWorm:
    def __init__(self, fail_on: int) -> None:
        self.calls = 0
        self.fail_on = fail_on

    def write(self, _fields: object) -> None:
        self.calls += 1
        if self.calls == self.fail_on:
            raise OSError("audit unavailable")


def _client(coordinator: arcrun.CallQueueCoordinator | None) -> tuple[TestClient, AuthConfig]:
    auth = AuthConfig({"viewer_token": "v" * 64, "operator_token": "o" * 64})
    app = Starlette(routes=routes)
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.user_store_factory = _Users
    app.state.queue_coordinator = coordinator
    app.state.queue_tenant_id = "tenant-a"
    app.state.audit = UIAuditLogger(enabled=False)
    app.state.audit_worm = _Worm()
    return TestClient(app), auth


def _session(auth: AuthConfig, email: str, did: str, role: str) -> dict[str, str]:
    token = auth.sessions.issue(email=email, did=did, role=role).token
    return {"Authorization": f"Bearer {token}"}


def test_static_operator_and_foreign_account_cannot_read_queue() -> None:
    queue = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    client, auth = _client(queue)
    static = {"Authorization": f"Bearer {auth.operator_token}"}
    foreign = _session(auth, "foreign@example.com", "did:arc:tenant-b:user/foreign", "operator")
    assert client.get("/api/queue/jobs", headers=static).status_code == 403
    assert client.get("/api/queue/jobs", headers=foreign).status_code == 403


def test_operator_reads_only_trusted_tenant_even_with_forged_query() -> None:
    queue = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    client, auth = _client(queue)
    operator = _session(auth, "operator@example.com", "did:arc:tenant-a:user/operator", "operator")
    response = client.get("/api/queue/jobs?tenant_id=tenant-b&limit=10", headers=operator)
    assert response.status_code == 400


def test_queue_control_refuses_viewer_and_missing_coordinator() -> None:
    queue = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    client, auth = _client(queue)
    viewer = _session(auth, "viewer@example.com", "did:arc:tenant-a:user/viewer", "viewer")
    assert (
        client.post("/api/queue/pause", headers=viewer, json={"expected_revision": 0}).status_code
        == 403
    )
    unavailable, auth2 = _client(None)
    operator = _session(
        auth2, "operator@example.com", "did:arc:tenant-a:user/operator", "operator"
    )
    assert unavailable.get("/api/queue/control", headers=operator).status_code == 503


def test_pause_is_revision_fenced_and_idempotent() -> None:
    queue = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    client, auth = _client(queue)
    operator = _session(auth, "operator@example.com", "did:arc:tenant-a:user/operator", "operator")
    first = client.post("/api/queue/pause", headers=operator, json={"expected_revision": 0})
    assert first.status_code == 200
    assert first.json()["paused"] is True
    assert first.json()["revision"] == 1
    stale = client.post("/api/queue/pause", headers=operator, json={"expected_revision": 0})
    assert stale.status_code == 409
    assert queue.control().revision == 1


def test_cancel_requires_scoped_job_and_reports_requested_not_confirmed() -> None:
    queue = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    client, auth = _client(queue)
    operator = _session(auth, "operator@example.com", "did:arc:tenant-a:user/operator", "operator")
    response = client.post(
        "/api/queue/cancel",
        headers=operator,
        json={"call_id": "foreign-call", "owner_id": "forged", "expected_version": 0},
    )
    assert response.status_code == 400


def test_limits_reject_unbounded_and_nonfinite_values() -> None:
    queue = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    client, auth = _client(queue)
    operator = _session(auth, "operator@example.com", "did:arc:tenant-a:user/operator", "operator")
    body: dict[str, Any] = {
        "expected_revision": 0,
        "max_concurrent": 0,
        "max_queued": 10,
        "wait_timeout": 60,
        "history_limit": 1000,
    }
    assert client.put("/api/queue/limits", headers=operator, json=body).status_code == 400
    body["max_concurrent"] = 2
    body["wait_timeout"] = "NaN"
    assert client.put("/api/queue/limits", headers=operator, json=body).status_code == 400


def test_streamed_oversize_body_does_not_pause_queue() -> None:
    queue = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    client, auth = _client(queue)
    operator = _session(auth, "operator@example.com", "did:arc:tenant-a:user/operator", "operator")
    response = client.post(
        "/api/queue/pause",
        headers={**operator, "Content-Type": "application/json", "Content-Length": "1"},
        content=(chunk for chunk in [b"{" + b" " * 5000 + b"}"]),
    )
    assert response.status_code == 400
    assert queue.control().paused is False


def test_missing_audit_or_mismatched_coordinator_tenant_fails_closed() -> None:
    queue = arcrun.CallQueueCoordinator(tenant_scope="tenant-b")
    client, auth = _client(queue)
    operator = _session(auth, "operator@example.com", "did:arc:tenant-a:user/operator", "operator")
    assert (
        client.post(
            "/api/queue/pause", headers=operator, json={"expected_revision": 0}
        ).status_code
        == 503
    )
    assert queue.control().paused is False
    scoped = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    available, auth2 = _client(scoped)
    operator2 = _session(
        auth2, "operator@example.com", "did:arc:tenant-a:user/operator", "operator"
    )
    available.app.state.audit_worm = None
    assert (
        available.post(
            "/api/queue/pause", headers=operator2, json={"expected_revision": 0}
        ).status_code
        == 503
    )
    assert scoped.control().paused is False


def test_audit_failure_before_mutation_refuses_and_after_mutation_is_uncertain() -> None:
    queue = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    client, auth = _client(queue)
    operator = _session(auth, "operator@example.com", "did:arc:tenant-a:user/operator", "operator")
    client.app.state.audit_worm = _FailingWorm(1)
    before = client.post("/api/queue/pause", headers=operator, json={"expected_revision": 0})
    assert before.status_code == 503
    assert queue.control().paused is False
    client.app.state.audit_worm = _FailingWorm(2)
    after = client.post("/api/queue/pause", headers=operator, json={"expected_revision": 0})
    assert after.status_code == 503
    assert after.json()["error"] == "queue_outcome_uncertain"
    assert queue.control().paused is True


def test_app_keeps_exact_injected_coordinator_and_tenant() -> None:
    coordinator = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    app = create_app(queue_coordinator=coordinator, queue_tenant_id="tenant-a")
    assert app.state.queue_coordinator is coordinator
    assert app.state.queue_tenant_id == "tenant-a"
    assert any(getattr(route, "path", None) == "/api/queue/jobs" for route in app.routes)


def test_app_refuses_mismatched_coordinator_scope_before_startup() -> None:
    coordinator = arcrun.CallQueueCoordinator(tenant_scope="tenant-b")
    with pytest.raises(ValueError, match="tenant scope mismatch"):
        create_app(queue_coordinator=coordinator, queue_tenant_id="tenant-a")


def test_app_refuses_noncanonical_queue_owner_epoch() -> None:
    coordinator = arcrun.CallQueueCoordinator(tenant_scope="tenant-a")
    with pytest.raises(ValueError, match="queue owner epoch"):
        create_app(
            queue_coordinator=coordinator,
            queue_tenant_id="tenant-a",
            queue_owner_epoch="01",
        )


def test_hosted_readiness_reports_missing_queue() -> None:
    app = create_app(hosted=True)
    response = TestClient(app).get("/api/ready")
    assert response.status_code == 503
    assert response.json()["components"]["queue"] == "unavailable"
