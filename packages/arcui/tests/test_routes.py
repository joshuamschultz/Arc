"""Integration tests for REST routes using httpx AsyncClient."""

import json
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

        resp = client.get(f"/api/traces/{rec.trace_id}", headers={"Authorization": "Bearer v"})
        assert resp.status_code == 200
        assert resp.json()["trace_id"] == rec.trace_id

    def test_get_trace_invalid_format(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/traces/nonexistent",
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

    def test_get_stats_unknown_agent_returns_404(self):
        _, client, _ = _make_app()
        resp = client.get(
            "/api/stats?agent_id=nonexistent",
            headers={"Authorization": "Bearer viewer-tok"},
        )
        assert resp.status_code == 404

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
    async def test_export_json(self, tmp_path: Path):
        from arcllm.trace_store import JSONLTraceStore, TraceRecord

        store = JSONLTraceStore(tmp_path / "ws")
        await store.append(TraceRecord(provider="anthropic", model="claude-sonnet-4"))

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        client = TestClient(app)

        resp = client.get("/api/export?format=json", headers={"Authorization": "Bearer v"})
        assert resp.status_code == 200
        assert resp.json()["count"] == 1

    async def test_export_csv(self, tmp_path: Path):
        from arcllm.trace_store import JSONLTraceStore, TraceRecord

        store = JSONLTraceStore(tmp_path / "ws")
        await store.append(TraceRecord(provider="anthropic", model="claude-sonnet-4"))

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        client = TestClient(app)

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


class TestStaticAssetsRegression:
    """Pin the every-link-from-index-loads contract.

    Regression guard: any future change that breaks the static mount, drops
    the CSS link, blocks `/assets/*` behind auth, or returns the wrong
    content type will fail these tests before reaching a browser.
    """

    # Every asset referenced from index.html. If you add a <link> or <script>
    # to index.html, add it here too — the test will catch missing files
    # before the browser does.
    _LINKED_ASSETS: tuple[tuple[str, str, str], ...] = (
        # path, content-type prefix, fingerprint string that must appear in body
        ("/assets/arc-platform.css", "text/css", "ARC Platform UI"),
        ("/assets/arc-shell.js", "text/javascript", ""),
        ("/assets/formatters.js", "text/javascript", ""),
        ("/assets/dom-batcher.js", "text/javascript", ""),
        ("/assets/store.js", "text/javascript", ""),
        ("/assets/ws-client.js", "text/javascript", "RobustWebSocket"),
        ("/assets/connection-ui.js", "text/javascript", ""),
        ("/assets/log-table.js", "text/javascript", ""),
    )

    def test_index_links_every_referenced_asset(self):
        """Each asset in _LINKED_ASSETS must appear by name in the served HTML.

        Catches accidental rename of a CSS or JS file without updating
        index.html.
        """
        _, client, _ = _make_app()
        html = client.get("/").text
        for path, _, _ in self._LINKED_ASSETS:
            asset_name = path.rsplit("/", 1)[-1]
            assert asset_name in html, f"index.html does not reference {asset_name}"

    def test_every_linked_asset_serves_200(self):
        """Every asset referenced from index.html resolves with 200.

        Catches a deleted/renamed file on disk that index.html still links.
        """
        _, client, _ = _make_app()
        for path, _, _ in self._LINKED_ASSETS:
            resp = client.get(path)
            assert resp.status_code == 200, (
                f"{path} returned {resp.status_code} — the static mount, "
                "the file on disk, or auth middleware is broken"
            )

    def test_every_linked_asset_has_correct_content_type(self):
        """Wrong Content-Type breaks browser parsing even if status=200.

        Browsers refuse to apply text/css when the Content-Type is text/plain,
        and execute text/javascript only when the type is right. This is the
        single most likely cause of a "stylesheet downloaded but not applied"
        bug.
        """
        _, client, _ = _make_app()
        for path, expected_prefix, _ in self._LINKED_ASSETS:
            resp = client.get(path)
            ctype = resp.headers.get("content-type", "")
            assert ctype.startswith(expected_prefix), (
                f"{path} served as {ctype!r}; browsers need {expected_prefix}"
            )

    def test_every_linked_asset_has_expected_fingerprint(self):
        """Each asset's body contains a known fingerprint — no empty file.

        Catches a partial-write or truncation bug where the file exists,
        serves 200, but is empty or wrong content.
        """
        _, client, _ = _make_app()
        for path, _, fingerprint in self._LINKED_ASSETS:
            if not fingerprint:
                continue
            resp = client.get(path)
            assert fingerprint in resp.text, (
                f"{path} did not contain expected fingerprint {fingerprint!r}"
            )

    def test_assets_are_unauthenticated(self):
        """`/assets/*` MUST pass through AuthMiddleware unchallenged.

        The browser fetches CSS/JS BEFORE any JS runs to set the Authorization
        header. If the middleware ever requires a bearer token on /assets/*,
        the CSS will silently fail to apply and the page will look unstyled.
        """
        _, client, _ = _make_app()
        # No Authorization header at all.
        for path, _, _ in self._LINKED_ASSETS:
            resp = client.get(path)
            assert resp.status_code == 200, (
                f"{path} returned {resp.status_code} without auth — "
                "AuthMiddleware should let static assets through"
            )

    def test_auth_bootstrap_script_runs_before_css_link(self):
        """SR-2: auth-token bootstrap script MUST be in <head> before any
        analytics or telemetry that could leak the URL hash.

        The CSS link comes after the bootstrap script — if their order is
        ever reversed (e.g. someone inlines a stylesheet that runs JS via
        @import url() side-effects), the token-strip race opens up.
        """
        _, client, _ = _make_app()
        html = client.get("/").text
        boot_idx = html.find("bootstrapAuth")
        css_idx = html.find("arc-platform.css")
        assert boot_idx > 0, "bootstrap script missing from index.html"
        assert css_idx > 0, "arc-platform.css link missing from index.html"
        assert boot_idx < css_idx, (
            "bootstrap script must appear before any <link> or external "
            "resource so it strips the URL hash first"
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
