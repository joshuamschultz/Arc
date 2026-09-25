"""Authenticated static report preview rejects active HTML and path escapes."""

from __future__ import annotations

import hashlib
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from arcui.auth import AuthConfig, AuthMiddleware
from arcui.report_authorization import ReportReadGrant, ReportReadRequest, ReportReadWorkerPool
from arcui.routes.agent_detail import report_preview
from arcui.routes.agent_detail.report_preview import get_report_preview


def _client(tmp_path: Path, *, is_operator: bool = True) -> tuple[TestClient, Path, str]:
    root = tmp_path / "ada"
    (root / "workspace").mkdir(parents=True)
    app = Starlette(routes=[Route("/api/agents/{id}/files/report", get_report_preview)])
    auth = AuthConfig({"viewer_token": "viewer", "operator_token": "operator"})
    app.add_middleware(AuthMiddleware, auth_config=auth)
    app.state.auth_config = auth
    app.state.roster_provider = lambda: [
        SimpleNamespace(agent_id="ada", did="did:arc:agent:ada", workspace_path=root)
    ]
    user = SimpleNamespace(
        email="ada@example.test", did="did:arc:user:ada", disabled=False, is_operator=is_operator
    )
    app.state.user_store_factory = lambda: SimpleNamespace(
        get=lambda email: user if email == user.email else None
    )
    app.state.report_read_authority = SimpleNamespace(
        authorize=lambda request: _grant(root, request)
    )
    app.state.report_read_workers = ReportReadWorkerPool()
    session = auth.sessions.issue(
        email=user.email, did=user.did, role="operator" if is_operator else "viewer"
    )
    return TestClient(app), root, session.token


def _grant(root: Path, request: ReportReadRequest) -> ReportReadGrant:
    return ReportReadGrant(
        caller_did=request.caller_did,
        agent_did=request.agent_did,
        report_id=request.report_id,
        source_id="tool:report-generator/run-1",
        source_sha256=hashlib.sha256(
            (root / request.root / request.report_id).read_bytes()
        ).hexdigest(),
    )


def test_report_keeps_static_table_and_blocks_active_content(tmp_path: Path) -> None:
    client, root, token = _client(tmp_path)
    (root / "workspace" / "report.html").write_text(
        '<h1>Monthly report</h1><table><tr><td style="color: red; '
        'background-image: url(https://evil.example/x)">42</td></tr></table>'
        '<script>fetch("https://evil.example/x")</script>'
        '<img src="https://evil.example/x" onerror="alert(1)">'
        '<a href="https://evil.example/x">leave</a><form action="https://evil.example/x">'
        "<button>send</button></form>",
        encoding="utf-8",
    )
    unauthenticated = client.get("/api/agents/ada/files/report?path=report.html")
    assert unauthenticated.status_code == 401
    response = client.get(
        "/api/agents/ada/files/report?path=report.html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert "Monthly report" in response.text
    assert "<table>" in response.text
    assert "42" in response.text
    for dangerous in ("evil.example", "<script", "<img", "<form", "<a", "onerror"):
        assert dangerous not in response.text
    assert "script-src 'none'" in response.headers["content-security-policy"]
    assert "sandbox" in response.headers["content-security-policy"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-arc-report-source"] == "tool:report-generator/run-1"
    assert (
        response.headers["x-arc-report-sha256"]
        == hashlib.sha256((root / "workspace" / "report.html").read_bytes()).hexdigest()
    )


def test_report_requires_deployment_policy_and_exact_provenance(tmp_path: Path) -> None:
    client, root, token = _client(tmp_path)
    (root / "workspace" / "report.html").write_text("<h1>Private</h1>")
    url = "/api/agents/ada/files/report?path=report.html"
    headers = {"Authorization": f"Bearer {token}"}
    client.app.state.report_read_authority = None
    absent = client.get(url, headers=headers)
    assert absent.status_code == 503
    assert "Private" not in absent.text

    client.app.state.report_read_authority = SimpleNamespace(authorize=lambda _request: None)
    denied = client.get(url, headers=headers)
    assert denied.status_code == 403
    assert "Private" not in denied.text

    client.app.state.report_read_authority = SimpleNamespace(
        authorize=lambda request: ReportReadGrant(
            caller_did=request.caller_did,
            agent_did=request.agent_did,
            report_id=request.report_id,
            source_id="tool:report-generator/run-1",
            source_sha256="0" * 64,
        )
    )
    stale = client.get(url, headers=headers)
    assert stale.status_code == 403
    assert "Private" not in stale.text


def test_report_rejects_cross_agent_grant(tmp_path: Path) -> None:
    client, root, token = _client(tmp_path)
    (root / "workspace" / "report.html").write_text("<h1>Private</h1>")
    client.app.state.report_read_authority = SimpleNamespace(
        authorize=lambda request: ReportReadGrant(
            caller_did=request.caller_did,
            agent_did="did:arc:agent:other",
            report_id=request.report_id,
            source_id="tool:report-generator/run-1",
            source_sha256=hashlib.sha256(b"<h1>Private</h1>").hexdigest(),
        )
    )
    denied = client.get(
        "/api/agents/ada/files/report?path=report.html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert denied.status_code == 403
    assert "Private" not in denied.text


def test_hung_report_authority_keeps_worker_occupied_after_deadline(tmp_path: Path) -> None:
    client, root, token = _client(tmp_path)
    (root / "workspace" / "report.html").write_text("<h1>Private</h1>")
    release = threading.Event()
    entered = threading.Event()

    def hung_authorize(_request: ReportReadRequest) -> None:
        entered.set()
        release.wait(2)
        return None

    client.app.state.report_read_workers.close()
    client.app.state.report_read_workers = ReportReadWorkerPool(
        max_workers=1, operation_timeout=0.05
    )
    client.app.state.report_read_authority = SimpleNamespace(authorize=hung_authorize)
    url = "/api/agents/ada/files/report?path=report.html"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with client:
            first = client.get(url, headers=headers)
            assert entered.is_set()
            assert first.status_code == 503
            second = client.get(url, headers=headers)
            assert second.status_code == 503
            assert "Private" not in second.text
    finally:
        release.set()
        client.app.state.report_read_workers.close()


def test_report_rejects_cross_agent_symlink_and_oversize(tmp_path: Path) -> None:
    client, root, token = _client(tmp_path)
    outside = tmp_path / "other.html"
    outside.write_text("secret", encoding="utf-8")
    (root / "workspace" / "linked.html").symlink_to(outside)
    symlink = client.get(
        "/api/agents/ada/files/report?path=linked.html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert symlink.status_code == 400
    (root / "workspace" / "large.html").write_bytes(b"x" * 1_048_577)
    large = client.get(
        "/api/agents/ada/files/report?path=large.html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert large.status_code == 413


def test_report_requires_identified_operator_session(tmp_path: Path) -> None:
    client, root, _ = _client(tmp_path)
    (root / "workspace" / "report.html").write_text("<h1>Private</h1>")
    static = client.get(
        "/api/agents/ada/files/report?path=report.html",
        headers={"Authorization": "Bearer viewer"},
    )
    assert static.status_code == 403
    assert "identified account session" in static.text
    viewer, viewer_root, viewer_token = _client(tmp_path / "viewer", is_operator=False)
    (viewer_root / "workspace" / "report.html").write_text("<h1>Private</h1>")
    denied = viewer.get(
        "/api/agents/ada/files/report?path=report.html",
        headers={"Authorization": f"Bearer {viewer_token}"},
    )
    assert denied.status_code == 403
    assert "Private" not in denied.text


def test_report_refuses_forged_session_and_handles_read_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root, token = _client(tmp_path)
    (root / "workspace" / "report.html").write_text("<h1>Private</h1>")
    client.app.state.user_store_factory = lambda: SimpleNamespace(
        get=lambda _email: SimpleNamespace(did="did:arc:user:other", disabled=False)
    )
    forged = client.get(
        "/api/agents/ada/files/report?path=report.html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert forged.status_code == 401
    client, root, token = _client(tmp_path / "again")
    (root / "workspace" / "report.html").write_text("<h1>Private</h1>")

    def fail_read(**_kwargs: object) -> None:
        raise OSError("secret path")

    monkeypatch.setattr(report_preview.fs_reader, "read_file", fail_read)
    failure = client.get(
        "/api/agents/ada/files/report?path=report.html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert failure.status_code == 503
    assert "secret path" not in failure.text


def test_report_refuses_linked_file_and_sanitizer_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client, root, token = _client(tmp_path)
    secret = root / "workspace" / "secret.key"
    secret.write_text("PRIVATE")
    (root / "workspace" / "report.html").hardlink_to(secret)
    linked = client.get(
        "/api/agents/ada/files/report?path=report.html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert linked.status_code == 404
    assert "PRIVATE" not in linked.text
    (root / "workspace" / "report.html").unlink()
    (root / "workspace" / "report.html").write_text("<h1>Report</h1>")

    def fail_clean(_content: str) -> str:
        raise ValueError("secret sanitizer detail")

    monkeypatch.setattr(report_preview, "_CLEANER", SimpleNamespace(clean=fail_clean))
    failure = client.get(
        "/api/agents/ada/files/report?path=report.html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert failure.status_code == 503
    assert "secret sanitizer detail" not in failure.text


def test_report_refuses_key_bytes_under_html_filename(tmp_path: Path) -> None:
    client, root, token = _client(tmp_path)
    (root / "workspace" / "report.html").write_text(
        "<pre>-----BEGIN PRIVATE KEY-----\nsecret\n-----END PRIVATE KEY-----</pre>",
        encoding="utf-8",
    )
    response = client.get(
        "/api/agents/ada/files/report?path=report.html",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403
    assert "BEGIN PRIVATE KEY" not in response.text
