"""End-to-end functional tests: LLM call → trace → API → stats.

These tests exercise the actual data path the user sees in the dashboard:
seed a trace store with realistic LLM call records, mount it on a real
ArcUI server, then query the same API endpoints the browser hits and
verify model identity, cost, tokens, and aggregation match.

Failure here means the dashboard is showing wrong data — a much sharper
signal than "page loads".
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from arcllm.trace_store import JSONLTraceStore, TraceRecord
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.federated_store import FederatedTraceStore
from arcui.server import create_app


def _record(
    *,
    agent: str = "agent_a",
    model: str = "claude-opus-4-6",
    provider: str = "anthropic",
    cost: float = 0.015,
    in_tokens: int = 1200,
    out_tokens: int = 340,
    latency_ms: float = 2400.0,
    ts: datetime | None = None,
) -> TraceRecord:
    """Build a realistic trace record for an LLM call."""
    return TraceRecord(
        provider=provider,
        model=model,
        agent_label=agent,
        timestamp=(ts or datetime.now(UTC)).isoformat(),
        cost_usd=cost,
        input_tokens=in_tokens,
        output_tokens=out_tokens,
        total_tokens=in_tokens + out_tokens,
        duration_ms=latency_ms,
        status="success",
    )


@pytest.fixture
def seeded_store(tmp_path: Path) -> JSONLTraceStore:
    """A single-workspace trace store with three realistic LLM calls."""
    store = JSONLTraceStore(tmp_path / "agent_a")
    return store


class TestSingleCallFlow:
    """One LLM call appended to the store appears in /api/traces with all fields."""

    async def test_appended_trace_returns_via_api(self, tmp_path: Path) -> None:
        """Append a trace; query /api/traces; verify exact match.

        This is the smallest possible end-to-end test — if it fails, the
        store/API/serialization chain is broken.
        """
        store = JSONLTraceStore(tmp_path / "agent_a")
        rec = _record(model="claude-opus-4-6", cost=0.0153, in_tokens=1234)
        await store.append(rec)

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        client = TestClient(app)

        resp = client.get(
            "/api/traces?limit=10",
            headers={"Authorization": "Bearer v"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["traces"]) == 1
        t = data["traces"][0]
        # Every UI-visible field must round-trip.
        assert t["model"] == "claude-opus-4-6"
        assert t["provider"] == "anthropic"
        assert t["agent_label"] == "agent_a"
        assert t["cost_usd"] == 0.0153
        assert t["input_tokens"] == 1234
        assert t["output_tokens"] == 340
        assert t["total_tokens"] == 1234 + 340
        assert t["status"] == "success"

    async def test_trace_id_is_unique_uuid(self, tmp_path: Path) -> None:
        """Each appended trace gets a unique 32-hex-char trace_id."""
        store = JSONLTraceStore(tmp_path / "agent_a")
        await store.append(_record())
        await store.append(_record())
        await store.append(_record())

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        client = TestClient(app)

        resp = client.get(
            "/api/traces?limit=10",
            headers={"Authorization": "Bearer v"},
        )
        ids = [t["trace_id"] for t in resp.json()["traces"]]
        assert len(ids) == 3
        assert len(set(ids)) == 3, "trace_ids must be unique"
        for tid in ids:
            assert len(tid) == 32, f"expected 32-char UUID hex, got {tid!r}"


class TestStatsAggregation:
    """Multiple calls aggregate correctly in /api/stats."""

    async def test_stats_sums_cost_and_tokens(self, tmp_path: Path) -> None:
        """Five calls; /api/stats reports the right totals — what the user sees."""
        store = JSONLTraceStore(tmp_path / "agent_a")
        for cost, in_t, out_t in [
            (0.01, 1000, 200),
            (0.02, 1500, 300),
            (0.015, 1200, 250),
            (0.025, 1800, 400),
            (0.012, 1100, 220),
        ]:
            await store.append(_record(cost=cost, in_tokens=in_t, out_tokens=out_t))

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        # Warm-start the aggregator from the store so /api/stats sees the data.
        client = TestClient(app)
        await app.state.aggregator.warm_start(store)

        resp = client.get(
            "/api/stats?window=24h",
            headers={"Authorization": "Bearer v"},
        )
        assert resp.status_code == 200
        s = resp.json()
        assert s["request_count"] == 5
        assert s["total_cost"] == pytest.approx(0.082, abs=1e-6)
        # Total tokens = sum(in + out) for each call:
        #   (1000+200) + (1500+300) + (1200+250) + (1800+400) + (1100+220) = 7970
        assert s["total_tokens"] == 7970
        assert s["error_count"] == 0

    async def test_stats_per_model_breakdown(self, tmp_path: Path) -> None:
        """Different models get separate rows in model_stats — what the
        Model Performance table shows."""
        store = JSONLTraceStore(tmp_path / "agent_a")
        for _ in range(3):
            await store.append(_record(model="claude-opus-4-6", cost=0.05))
        for _ in range(7):
            await store.append(_record(model="claude-haiku-4-5", cost=0.005))

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=store)
        client = TestClient(app)
        await app.state.aggregator.warm_start(store)

        resp = client.get(
            "/api/stats?window=24h",
            headers={"Authorization": "Bearer v"},
        )
        s = resp.json()
        models = s["model_stats"]
        assert "claude-opus-4-6" in models
        assert "claude-haiku-4-5" in models
        assert models["claude-opus-4-6"]["request_count"] == 3
        assert models["claude-haiku-4-5"]["request_count"] == 7
        assert models["claude-opus-4-6"]["total_cost"] == pytest.approx(0.15)
        assert models["claude-haiku-4-5"]["total_cost"] == pytest.approx(0.035)


class TestFederatedQueryFlow:
    """Multi-workspace registry → FederatedTraceStore → /api/traces."""

    async def test_three_agents_all_appear_in_traces(self, tmp_path: Path) -> None:
        """Three agents each with one call. /api/traces returns all three."""
        agents = ["agent_a", "agent_b", "agent_c"]
        stores = []
        for agent in agents:
            store = JSONLTraceStore(tmp_path / agent)
            await store.append(_record(agent=agent))
            stores.append(store)

        federated = FederatedTraceStore(stores)

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=federated)
        client = TestClient(app)

        resp = client.get(
            "/api/traces?limit=10",
            headers={"Authorization": "Bearer v"},
        )
        assert resp.status_code == 200
        traces = resp.json()["traces"]
        assert len(traces) == 3
        labels = {t["agent_label"] for t in traces}
        assert labels == set(agents)

    async def test_agent_filter_isolates_one_agent(self, tmp_path: Path) -> None:
        """`?agent=agent_b` returns only agent_b's records — filter must apply
        across the federation, not leak."""
        for agent in ["agent_a", "agent_b"]:
            store = JSONLTraceStore(tmp_path / agent)
            await store.append(_record(agent=agent))
            await store.append(_record(agent=agent))

        stores = [
            JSONLTraceStore(tmp_path / "agent_a"),
            JSONLTraceStore(tmp_path / "agent_b"),
        ]
        federated = FederatedTraceStore(stores)

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth, trace_store=federated)
        client = TestClient(app)

        resp = client.get(
            "/api/traces?limit=10&agent=agent_b",
            headers={"Authorization": "Bearer v"},
        )
        assert resp.status_code == 200
        traces = resp.json()["traces"]
        assert len(traces) == 2
        for t in traces:
            assert t["agent_label"] == "agent_b", "agent filter leaked another agent's records"


class TestBrowserBootstrapAuthFlow:
    """SPEC-019: token in URL hash → localStorage → Authorization header.

    These tests simulate exactly what the browser does: extract a token from
    the URL hash, send it in the Authorization header on subsequent requests.
    The server must authenticate identically whether the token reached the
    browser via URL bootstrap or manual paste.
    """

    def test_bootstrap_token_authenticates_api_calls(self, tmp_path: Path) -> None:
        """Token from URL hash (server-issued) authenticates API calls when
        sent in Authorization: Bearer header."""
        viewer_token = "viewer-from-bootstrap"
        auth = AuthConfig({"viewer_token": viewer_token, "operator_token": "op"})
        app = create_app(auth_config=auth)
        client = TestClient(app)

        # Same token the bootstrap script writes to localStorage and the
        # page sends as Bearer on every fetch.
        resp = client.get(
            "/api/stats?window=24h",
            headers={"Authorization": f"Bearer {viewer_token}"},
        )
        assert resp.status_code == 200

    def test_bootstrap_token_authenticates_websocket(self, tmp_path: Path) -> None:
        """Same token → WebSocket first-message auth succeeds.

        This is the exact path the dashboard takes: read localStorage, pass
        to RobustWebSocket, which sends `{"token": "..."}` as first frame.
        """
        viewer_token = "viewer-from-bootstrap"
        auth = AuthConfig({"viewer_token": viewer_token, "operator_token": "op"})
        app = create_app(auth_config=auth)
        client = TestClient(app)

        with client.websocket_connect("/ws") as ws:
            ws.send_text(json.dumps({"token": viewer_token}))
            resp = ws.receive_json()
            assert resp["type"] == "auth_ok"
            assert resp["role"] == "viewer"

    def test_no_token_fails_auth_predictably(self, tmp_path: Path) -> None:
        """Empty Authorization header → 401, no silent failure.

        The user MUST see a 401 (which the dashboard surfaces as "auth
        error") rather than a successful response with empty data — that
        masks the misconfiguration as "no traces yet".
        """
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        client = TestClient(app)

        resp = client.get("/api/stats?window=24h")
        assert resp.status_code == 401


class TestDashboardCacheControl:
    """Index HTML must be served with no-store so a stale tab can't outlive
    a server restart with dead tokens."""

    def test_index_sends_no_store(self) -> None:
        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        client = TestClient(app)

        resp = client.get("/")
        assert resp.status_code == 200
        cc = resp.headers.get("cache-control", "")
        assert "no-store" in cc, (
            f"index served with Cache-Control={cc!r} — a tab cached across "
            "an `arc ui start` restart will resurrect a dead viewer token"
        )
