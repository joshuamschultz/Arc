"""Integration tests for REST routes using httpx AsyncClient."""

import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

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


def _seed_spool(data_dir: Path, *, model: str = "claude", outcome: str = "ok") -> str:
    """Write one llm_call to the durable spool; return its trace_id (record_id)."""
    from arcstore.records import SpoolRecord
    from arcstore.spool import record as spool_record

    spool = data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    rec = SpoolRecord(
        kind="llm_call",
        actor_did="did:arc:test:exec/aabbccdd",
        request_id="req-1",
        model=model,
        prompt_tokens=100,
        completion_tokens=50,
        cost_usd=0.0015,
        latency_ms=42.0,
        outcome=outcome,
    )
    spool_record(rec, path=spool / "operational-2026-05-31.jsonl")
    return rec.record_id


class TestHealthRoute:
    def test_health_no_auth_needed(self):
        _, client, _ = _make_app()
        # Health endpoint is exempt from auth
        resp = client.get("/api/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_health_works_with_auth_too(self):
        _, client, _ = _make_app()
        resp = client.get("/api/health", headers={"Authorization": "Bearer viewer-tok"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"


class TestInfoRoute:
    def test_info_returns_agent_metadata(self):
        auth = AuthConfig({"viewer_token": "viewer-tok", "operator_token": "operator-tok"})
        info = {
            "name": "my-agent",
            "did": "did:arc:local:executor/abc123",
            "model": "anthropic/claude-sonnet-4-6",
        }
        app = create_app(auth_config=auth, agent_info=info)
        client = TestClient(app)
        resp = client.get("/api/info", headers={"Authorization": "Bearer viewer-tok"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "my-agent"
        assert data["did"] == "did:arc:local:executor/abc123"
        assert data["model"] == "anthropic/claude-sonnet-4-6"

    def test_info_empty_when_no_agent_info(self):
        _, client, _ = _make_app()
        resp = client.get("/api/info", headers={"Authorization": "Bearer viewer-tok"})
        assert resp.status_code == 200
        assert resp.json() == {}


class TestTracesRoute:
    def test_list_traces_empty(self):
        _, client, _ = _make_app()
        resp = client.get("/api/traces", headers={"Authorization": "Bearer viewer-tok"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["traces"] == []
        assert data["cursor"] is None

    def test_list_traces_with_store(self, _isolated_arc_data_dir: Path):
        # SPEC-026 FR-5: arcui reads the durable spool via the Observe plane.
        # Seed a spool record, then let the lifespan backfill it into the mirror.
        _seed_spool(_isolated_arc_data_dir, model="claude-sonnet-4")

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
            resp = client.get("/api/traces", headers={"Authorization": "Bearer v"})
        assert resp.status_code == 200
        assert len(resp.json()["traces"]) == 1

    def test_get_trace_by_id(self, _isolated_arc_data_dir: Path):
        trace_id = _seed_spool(_isolated_arc_data_dir, model="claude-sonnet-4")

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
            resp = client.get(
                f"/api/traces/{trace_id}", headers={"Authorization": "Bearer v"}
            )
        assert resp.status_code == 200
        assert resp.json()["trace_id"] == trace_id

    def test_get_trace_invalid_format(self):
        # _VALID_TRACE_ID_RE rejects disallowed chars (whitespace, `;<>`,
        # control bytes) and lengths over 128. Use a clearly-bad id here
        # so the test exercises validation rather than not-found.
        _, client, _ = _make_app()
        resp = client.get(
            "/api/traces/bad;DROP TABLE traces",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 400

    def test_get_trace_not_found(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/traces/00000000000000000000000000000000",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 404

    def test_get_trace_accepts_real_world_id_formats(self):
        """Trace IDs flow from many producers (chat_handler emits
        ``chat-NNN``, demo orchestrators emit ``run-abc:role``,
        ui_reporter emits ``trace-NNN-N``). All must pass validation
        and reach the store (404 means it got past the regex)."""
        _, client, _ = _make_app()
        for trace_id in (
            "chat-1779479813978",
            "run-abc1234:approver",
            "trace-1779479813-7",
            "550e8400-e29b-41d4-a716-446655440000",
            "deadbeefdeadbeefdeadbeefdeadbeef",
        ):
            resp = client.get(
                f"/api/traces/{trace_id}",
                headers={"Authorization": "Bearer viewer-tok"},
            )
            assert resp.status_code != 400, f"trace_id {trace_id!r} unexpectedly rejected"


class TestConfigRoute:
    def test_get_config_no_controller(self):
        _, client, _ = _make_app()
        resp = client.get("/api/config", headers={"Authorization": "Bearer viewer-tok"})
        assert resp.status_code == 404

    def test_get_config_with_controller(self):
        from arcllm.config_controller import ConfigController

        ctrl = ConfigController({"model": "claude-sonnet-4", "temperature": 0.7})
        _, client, _ = _make_app(config_controller=ctrl)

        resp = client.get("/api/config", headers={"Authorization": "Bearer viewer-tok"})
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
        resp = client.get("/api/stats", headers={"Authorization": "Bearer viewer-tok"})
        assert resp.status_code == 200
        data = resp.json()
        assert "request_count" in data

    def test_get_stats_unknown_agent_returns_empty(self):
        """Disk-roster agents that aren't currently connected return an
        empty-but-well-formed response, not 404 — the agent-detail UI
        renders 'no activity yet' instead of error-ing."""
        _, client, _ = _make_app()
        resp = client.get(
            "/api/stats?agent_id=nonexistent",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("request_count", 0) == 0

    def test_get_stats_invalid_window_400(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/stats?window=invalid",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 400
        assert "Invalid window" in resp.json()["error"]

    def test_get_timeseries(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/stats/timeseries",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "buckets" in data

    def test_get_performance(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/performance",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "models" in data
        assert "agents" in data

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
        resp = client.get("/api/budget", headers={"Authorization": "Bearer viewer-tok"})
        assert resp.status_code == 200
        assert resp.json()["budgets"] == []


class TestExportRoute:
    def test_export_json(self, _isolated_arc_data_dir: Path):
        # SPEC-026 FR-5: export reads from the arcstore Observe mirror.
        _seed_spool(_isolated_arc_data_dir, model="claude-sonnet-4")

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
            resp = client.get("/api/export?format=json", headers={"Authorization": "Bearer v"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    def test_export_csv(self, _isolated_arc_data_dir: Path):
        # SPEC-026 FR-5: export reads from the arcstore Observe mirror.
        _seed_spool(_isolated_arc_data_dir, model="claude-sonnet-4")

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
            resp = client.get("/api/export?format=csv", headers={"Authorization": "Bearer v"})
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


def _referenced_assets(html: str) -> list[str]:
    """Every `/assets/...` URL the built index.html references (Vite emits
    content-hashed filenames, so we read them from the HTML rather than
    hardcoding names)."""
    return re.findall(r'(?:src|href)="(/assets/[^"]+)"', html)


class TestDashboardRoute:
    def test_serves_index_html(self):
        _, client, _ = _make_app()
        resp = client.get("/")
        assert resp.status_code == 200
        assert '<div id="root">' in resp.text  # React mount point
        assert "/assets/index-" in resp.text  # built bundle is referenced

    def test_serves_bundled_assets(self):
        _, client, _ = _make_app()
        assets = _referenced_assets(client.get("/").text)
        assert assets, "index.html references no /assets bundles"
        for path in assets:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} -> {resp.status_code}"
            assert resp.content, f"{path} served empty"


class TestStaticAssetsRegression:
    """Every asset the built index.html references must load with the right
    MIME and a non-empty body, served unauthenticated. Hash-proof: the asset
    names are read from the served HTML, not hardcoded.
    """

    def _assets(self, client) -> list[str]:
        return _referenced_assets(client.get("/").text)

    def test_index_references_js_and_css_bundles(self):
        _, client, _ = _make_app()
        assets = self._assets(client)
        assert any(a.endswith(".js") for a in assets), "no JS bundle referenced"
        assert any(a.endswith(".css") for a in assets), "no CSS bundle referenced"

    def test_every_referenced_asset_serves_200(self):
        _, client, _ = _make_app()
        for path in self._assets(client):
            resp = client.get(path)
            assert resp.status_code == 200, (
                f"{path} returned {resp.status_code} — the static mount or the "
                "file on disk is broken"
            )

    def test_bundles_have_correct_content_type(self):
        """Wrong Content-Type breaks browser parsing even at status 200."""
        _, client, _ = _make_app()
        prefixes = {".js": "text/javascript", ".css": "text/css"}
        for path in self._assets(client):
            for ext, prefix in prefixes.items():
                if path.endswith(ext):
                    ctype = client.get(path).headers.get("content-type", "")
                    assert ctype.startswith(prefix), (
                        f"{path} served as {ctype!r}; browsers need {prefix}"
                    )

    def test_assets_are_unauthenticated(self):
        """`/assets/*` MUST pass AuthMiddleware unchallenged — the browser
        fetches CSS/JS before any JS can set the Authorization header."""
        _, client, _ = _make_app()
        for path in self._assets(client):  # no Authorization header
            resp = client.get(path)
            assert resp.status_code == 200, (
                f"{path} returned {resp.status_code} without auth — "
                "AuthMiddleware should let static assets through"
            )


class TestArcllmConfigRoute:
    """Tests for /api/arcllm-config (GET/PATCH) — config.toml management."""

    def _make_config_file(self, tmp_path: Path) -> Path:
        """Create a test config.toml and patch _get_config_dir."""
        config = tmp_path / "config.toml"
        config.write_text(
            '[defaults]\nprovider = "anthropic"\ntemperature = 0.7\n'
            "max_tokens = 4096\n\n"
            '[vault]\nbackend = ""\ncache_ttl_seconds = 300\n'
            'url = ""\nregion = ""\n\n'
            "[modules.telemetry]\nenabled = true\n"
            'log_level = "INFO"\n\n'
            "[modules.retry]\nenabled = true\nmax_retries = 3\n"
        )
        return config

    def test_get_config_returns_json(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.get(
                "/api/arcllm-config",
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["defaults"]["provider"] == "anthropic"
        assert data["defaults"]["temperature"] == 0.7
        assert data["defaults"]["max_tokens"] == 4096

    def test_get_config_viewer_redacts_vault(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.get(
                "/api/arcllm-config",
                headers={"Authorization": "Bearer viewer-tok"},
            )
        assert resp.status_code == 200
        data = resp.json()
        # Vault fields should be redacted for viewer
        assert data["vault"]["backend"] == "***"
        assert data["vault"]["url"] == "***"
        # Non-sensitive fields should be visible
        assert data["defaults"]["provider"] == "anthropic"

    def test_get_config_arcllm_not_installed(self):
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=None,
        ):
            _, client, _ = _make_app()
            resp = client.get(
                "/api/arcllm-config",
                headers={"Authorization": "Bearer viewer-tok"},
            )
        assert resp.status_code == 503
        assert "not installed" in resp.json()["error"]

    def test_get_config_file_not_found(self, tmp_path: Path):
        missing = tmp_path / "nonexistent.toml"
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=missing,
        ):
            _, client, _ = _make_app()
            resp = client.get(
                "/api/arcllm-config",
                headers={"Authorization": "Bearer viewer-tok"},
            )
        assert resp.status_code == 404

    def test_patch_config_requires_operator(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.patch(
                "/api/arcllm-config",
                content=json.dumps({"defaults": {"temperature": 0.5}}),
                headers={"Authorization": "Bearer viewer-tok"},
            )
        assert resp.status_code == 403

    def test_patch_config_operator_succeeds(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.patch(
                "/api/arcllm-config",
                content=json.dumps({"defaults": {"temperature": 0.5}}),
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 200
        assert resp.json()["defaults"]["temperature"] == 0.5
        # Verify file was actually updated (atomic write)
        import tomlkit

        with open(config) as f:
            doc = tomlkit.load(f)
        assert doc["defaults"]["temperature"] == 0.5

    def test_patch_config_invalid_json(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.patch(
                "/api/arcllm-config",
                content=b"not json",
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 400
        assert "Invalid JSON" in resp.json()["error"]

    def test_patch_config_non_dict_body(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.patch(
                "/api/arcllm-config",
                content=json.dumps([1, 2, 3]),
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 400
        assert "JSON object" in resp.json()["error"]

    def test_patch_config_rejects_unknown_section(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.patch(
                "/api/arcllm-config",
                content=json.dumps({"malicious": {"evil": True}}),
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 400
        assert "Unknown config section" in resp.json()["error"]

    def test_patch_config_rejects_unknown_key(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.patch(
                "/api/arcllm-config",
                content=json.dumps({"defaults": {"evil_key": "value"}}),
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 400
        assert "Unknown key" in resp.json()["error"]

    def test_patch_config_rejects_unknown_module(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.patch(
                "/api/arcllm-config",
                content=json.dumps({"modules": {"evil_mod": {"enabled": True}}}),
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 400
        assert "Unknown module" in resp.json()["error"]

    def test_patch_config_deep_merge(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            # Update nested module config
            resp = client.patch(
                "/api/arcllm-config",
                content=json.dumps({"modules": {"telemetry": {"log_level": "DEBUG"}}}),
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 200
        # Module's existing enabled=true should be preserved
        data = resp.json()
        assert data["modules"]["telemetry"]["enabled"] is True
        assert data["modules"]["telemetry"]["log_level"] == "DEBUG"

    def test_patch_config_arcllm_not_installed(self):
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=None,
        ):
            _, client, _ = _make_app()
            resp = client.patch(
                "/api/arcllm-config",
                content=json.dumps({"defaults": {"temperature": 0.5}}),
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 503

    def test_patch_config_body_too_large(self, tmp_path: Path):
        config = self._make_config_file(tmp_path)
        with patch(
            "arcui.routes.arcllm_config._get_config_path",
            return_value=config,
        ):
            _, client, _ = _make_app()
            resp = client.patch(
                "/api/arcllm-config",
                content=b"x" * 100_000,
                headers={"Authorization": "Bearer operator-tok"},
            )
        assert resp.status_code == 413


class TestArcllmConfigUnit:
    """Unit tests for arcllm_config helper functions."""

    def test_tomlkit_to_plain(self):
        import tomlkit

        from arcui.routes.arcllm_config import _tomlkit_to_plain

        doc = tomlkit.parse(
            '[defaults]\nprovider = "test"\ntemperature = 0.5\nmax_tokens = 100\nenabled = true\n'
        )
        plain = _tomlkit_to_plain(dict(doc))
        assert isinstance(plain["defaults"]["provider"], str)
        assert isinstance(plain["defaults"]["temperature"], float)
        assert isinstance(plain["defaults"]["max_tokens"], int)
        assert isinstance(plain["defaults"]["enabled"], bool)
        # Verify values are correct
        assert plain["defaults"]["provider"] == "test"
        assert plain["defaults"]["temperature"] == 0.5
        assert plain["defaults"]["max_tokens"] == 100
        assert plain["defaults"]["enabled"] is True

    def test_deep_merge(self):
        from arcui.routes.arcllm_config import _deep_merge

        target = {"a": {"x": 1, "y": 2}, "b": 3}
        source = {"a": {"y": 99, "z": 100}}
        _deep_merge(target, source)
        assert target == {"a": {"x": 1, "y": 99, "z": 100}, "b": 3}

    def test_validate_updates_valid(self):
        from arcui.routes.arcllm_config import _validate_updates

        assert _validate_updates({"defaults": {"temperature": 0.5}}) is None
        assert _validate_updates({"modules": {"retry": {"max_retries": 5}}}) is None

    def test_validate_updates_rejects_unknown(self):
        from arcui.routes.arcllm_config import _validate_updates

        assert "Unknown config section" in _validate_updates({"bad": {}})
        assert "Unknown key" in _validate_updates({"defaults": {"bad_key": 1}})
        assert "Unknown module" in _validate_updates({"modules": {"bad_mod": {}}})


def _seed_tool_and_spawn(data_dir: Path) -> None:
    """Write a tool_event + spawn_event + a child llm_call to the durable spool."""
    from arcstore.records import SpoolRecord
    from arcstore.spool import record as spool_record

    spool = data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    p = spool / "operational-2026-05-31.jsonl"
    spool_record(SpoolRecord(kind="tool_event", actor_did="did:c", request_id="run-1",
                             tool_name="web.fetch", phase="start", args_digest="a" * 64), path=p)
    spool_record(SpoolRecord(kind="spawn_event", actor_did="did:child",
                             parent_did="did:parent", child_did="did:child",
                             role="researcher", depth=1, outcome="allow"), path=p)
    spool_record(SpoolRecord(kind="llm_call", actor_did="did:child", request_id="run-1",
                             model="claude", agent_label="researcher:d1", cost_usd=0.01,
                             prompt_tokens=10, completion_tokens=5, outcome="ok"), path=p)


class TestToolAndLineageRoutes:
    def test_tool_and_lineage_routes(self, _isolated_arc_data_dir: Path):
        """Task 4.5 — read routes return tool timeline + spawn tree + identity cost JSON."""
        _seed_tool_and_spawn(_isolated_arc_data_dir)
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
            tl = client.get("/api/runs/run-1/timeline", headers={"Authorization": "Bearer v"})
            assert tl.status_code == 200
            kinds = {e["kind"] for e in tl.json()["timeline"]}
            assert {"tool_event", "llm_call"} <= kinds

            tree = client.get("/api/spawn-tree?root=did:parent",
                              headers={"Authorization": "Bearer v"})
            assert tree.status_code == 200
            assert tree.json()["tree"]["did"] == "did:parent"

            ident = client.get("/api/stats/by-identity?window=24h",
                               headers={"Authorization": "Bearer v"})
            assert ident.status_code == 200
            labels = {r["identity"] for r in ident.json()["identities"]}
            assert "researcher:d1" in labels

    def test_runs_route_lists_real_runs(self, _isolated_arc_data_dir: Path):
        """GET /api/runs enumerates runs by request_id (not session files)."""
        _seed_tool_and_spawn(_isolated_arc_data_dir)
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
            resp = client.get("/api/runs", headers={"Authorization": "Bearer v"})
            assert resp.status_code == 200
            runs = resp.json()["runs"]
            run = next(r for r in runs if r["run_id"] == "run-1")
            assert run["tool_calls"] == 1
            assert run["llm_calls"] == 1
            assert run["total_tokens"] == 15
