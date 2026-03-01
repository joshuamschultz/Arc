"""Integration tests for REST routes using httpx AsyncClient."""

import json
from pathlib import Path
from unittest.mock import MagicMock

from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app


def _make_app(
    tmp_path: Path | None = None,
    config_controller: object | None = None,
) -> tuple:
    """Build a test app with known auth tokens."""
    auth = AuthConfig({"viewer_token": "viewer-tok", "operator_token": "operator-tok"})
    app = create_app(auth_config=auth, config_controller=config_controller)
    client = TestClient(app)
    return app, client, auth


class TestHealthRoute:
    def test_health_no_auth_needed(self):
        _, client, _ = _make_app()
        # Health endpoint is at /api/health but needs auth
        resp = client.get(
            "/api/health", headers={"Authorization": "Bearer viewer-tok"}
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestTracesRoute:
    def test_list_traces_empty(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/traces", headers={"Authorization": "Bearer viewer-tok"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["traces"] == []
        assert data["cursor"] is None

    async def test_list_traces_with_store(self, tmp_path: Path):
        from arcllm.trace_store import JSONLTraceStore, TraceRecord

        store = JSONLTraceStore(tmp_path / "ws")
        rec = TraceRecord(provider="anthropic", model="claude-sonnet-4")
        await store.append(rec)

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        client = TestClient(app)

        resp = client.get("/api/traces", headers={"Authorization": "Bearer v"})
        assert resp.status_code == 200
        assert len(resp.json()["traces"]) == 1

    async def test_get_trace_by_id(self, tmp_path: Path):
        from arcllm.trace_store import JSONLTraceStore, TraceRecord

        store = JSONLTraceStore(tmp_path / "ws")
        rec = TraceRecord(provider="anthropic", model="claude-sonnet-4")
        await store.append(rec)

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        client = TestClient(app)

        resp = client.get(
            f"/api/traces/{rec.trace_id}", headers={"Authorization": "Bearer v"}
        )
        assert resp.status_code == 200
        assert resp.json()["trace_id"] == rec.trace_id

    def test_get_trace_not_found(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/traces/nonexistent",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 404


class TestConfigRoute:
    def test_get_config_no_controller(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/config", headers={"Authorization": "Bearer viewer-tok"}
        )
        assert resp.status_code == 404

    def test_get_config_with_controller(self):
        from arcllm.config_controller import ConfigController

        ctrl = ConfigController({"model": "claude-sonnet-4", "temperature": 0.7})
        _, client, _ = _make_app(config_controller=ctrl)

        resp = client.get(
            "/api/config", headers={"Authorization": "Bearer viewer-tok"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["model"] == "claude-sonnet-4"
        assert data["temperature"] == 0.7

    def test_patch_config_requires_operator(self):
        from arcllm.config_controller import ConfigController

        ctrl = ConfigController({"model": "claude-sonnet-4"})
        _, client, _ = _make_app(config_controller=ctrl)

        resp = client.patch(
            "/api/config",
            content=json.dumps({"temperature": 0.3}),
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 403

    def test_patch_config_operator_succeeds(self):
        from arcllm.config_controller import ConfigController

        ctrl = ConfigController({"model": "claude-sonnet-4"})
        _, client, _ = _make_app(config_controller=ctrl)

        resp = client.patch(
            "/api/config",
            content=json.dumps({"temperature": 0.3}),
            headers={"Authorization": "Bearer operator-tok"},
        )
        assert resp.status_code == 200
        assert resp.json()["temperature"] == 0.3


class TestStatsRoute:
    def test_get_stats(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/stats", headers={"Authorization": "Bearer viewer-tok"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "request_count" in data

    def test_get_circuit_breakers_empty(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/circuit-breakers",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200
        assert resp.json()["circuit_breakers"] == []

    def test_get_budget_empty(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/budget", headers={"Authorization": "Bearer viewer-tok"}
        )
        assert resp.status_code == 200
        assert resp.json()["budgets"] == []


class TestExportRoute:
    async def test_export_json(self, tmp_path: Path):
        from arcllm.trace_store import JSONLTraceStore, TraceRecord

        store = JSONLTraceStore(tmp_path / "ws")
        await store.append(
            TraceRecord(provider="anthropic", model="claude-sonnet-4")
        )

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        client = TestClient(app)

        resp = client.get(
            "/api/export?format=json", headers={"Authorization": "Bearer v"}
        )
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    async def test_export_csv(self, tmp_path: Path):
        from arcllm.trace_store import JSONLTraceStore, TraceRecord

        store = JSONLTraceStore(tmp_path / "ws")
        await store.append(
            TraceRecord(provider="anthropic", model="claude-sonnet-4")
        )

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        client = TestClient(app)

        resp = client.get(
            "/api/export?format=csv", headers={"Authorization": "Bearer v"}
        )
        assert resp.status_code == 200
        assert "trace_id" in resp.text  # CSV header


class TestCostEfficiencyRoute:
    def test_get_cost_efficiency(self):
        _, client, _ = _make_app()
        # Ingest some data into the aggregator
        resp = client.get(
            "/api/cost-efficiency",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data
        assert "window" in data


class TestInputValidation:
    """Verify limit parameter validation and error handling."""

    def test_traces_invalid_limit_returns_400(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/traces?limit=abc",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 400
        assert "Invalid limit" in resp.json()["error"]

    def test_traces_negative_limit_clamped(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/traces?limit=-5",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        # Clamped to 1, should succeed (empty store returns empty)
        assert resp.status_code == 200

    def test_traces_huge_limit_clamped(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/traces?limit=999999",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200  # Clamped to 500

    def test_export_invalid_limit_returns_400(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/export?limit=abc",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 400
        assert "Invalid limit" in resp.json()["error"]

    def test_config_patch_unexpected_error_returns_500(self):

        ctrl = MagicMock()
        ctrl.get_snapshot.return_value = MagicMock(model_dump=lambda: {})
        ctrl.patch.side_effect = RuntimeError("unexpected")

        _, client, _ = _make_app(config_controller=ctrl)
        resp = client.patch(
            "/api/config",
            content=json.dumps({"temperature": 0.3}),
            headers={"Authorization": "Bearer operator-tok"},
        )
        assert resp.status_code == 500
        assert "Internal error" in resp.json()["error"]
        # Should NOT contain "RuntimeError" or internal details
        assert "unexpected" not in resp.json()["error"]

    def test_config_patch_value_error_returns_400(self):

        ctrl = MagicMock()
        ctrl.get_snapshot.return_value = MagicMock(model_dump=lambda: {})
        ctrl.patch.side_effect = ValueError("Invalid key: foo")

        _, client, _ = _make_app(config_controller=ctrl)
        resp = client.patch(
            "/api/config",
            content=json.dumps({"foo": "bar"}),
            headers={"Authorization": "Bearer operator-tok"},
        )
        assert resp.status_code == 400
        assert "Invalid key" in resp.json()["error"]


class TestDashboardRoute:
    def test_serves_index_html(self):
        _, client, _ = _make_app()
        resp = client.get("/")
        assert resp.status_code == 200
        assert "LLM Telemetry" in resp.text
        assert "arc-platform.css" in resp.text

    def test_serves_static_css(self):
        _, client, _ = _make_app()
        resp = client.get("/assets/arc-platform.css")
        assert resp.status_code == 200
        assert "ARC Platform UI" in resp.text

    def test_serves_static_js(self):
        _, client, _ = _make_app()
        resp = client.get("/assets/ws-client.js")
        assert resp.status_code == 200
        assert "RobustWebSocket" in resp.text
