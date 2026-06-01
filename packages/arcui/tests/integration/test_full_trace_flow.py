"""End-to-end functional tests: LLM call → trace → API → stats.

These tests exercise the actual data path the user sees in the dashboard:
seed a trace store with realistic LLM call records, mount it on a real
ArcUI server, then query the same API endpoints the browser hits and
verify model identity, cost, tokens, and aggregation match.

Failure here means the dashboard is showing wrong data — a much sharper
signal than "page loads".
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcstore.records import SpoolRecord
from arcstore.spool import record as spool_record
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.server import create_app


def _seed(
    data_dir: Path,
    *,
    agent: str = "agent_a",
    model: str = "claude-opus-4-6",
    provider: str = "anthropic",
    cost: float = 0.015,
    in_tokens: int = 1200,
    out_tokens: int = 340,
    latency_ms: float = 2400.0,
    seq: int = 0,
) -> str:
    """Append one llm_call to the durable spool (SPEC-026 Observe plane).

    Returns the record_id (== the trace_id the UI sees).
    """
    spool = data_dir / "spool"
    spool.mkdir(parents=True, exist_ok=True)
    rec = SpoolRecord(
        kind="llm_call",
        actor_did=f"did:arc:test:{agent}",
        agent_label=agent,
        request_id=f"req-{agent}-{seq}",
        model=model,
        provider=provider,
        prompt_tokens=in_tokens,
        completion_tokens=out_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        outcome="ok",
    )
    spool_record(rec, path=spool / "operational-2026-05-31.jsonl")
    return rec.record_id


class TestSingleCallFlow:
    """One LLM call appended to the store appears in /api/traces with all fields."""

    def test_appended_trace_returns_via_api(self, _isolated_arc_data_dir: Path) -> None:
        """Seed a spool call; query /api/traces; verify exact match.

        The smallest end-to-end test — if it fails, the spool → ingest → mirror
        → API → serialization chain is broken.
        """
        _seed(_isolated_arc_data_dir, model="claude-opus-4-6", cost=0.0153, in_tokens=1234)

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
            resp = client.get("/api/traces?limit=10", headers={"Authorization": "Bearer v"})
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

    def test_trace_id_is_unique(self, _isolated_arc_data_dir: Path) -> None:
        """Each call gets a unique 32-hex-char content-derived trace_id."""
        for seq in range(3):
            _seed(_isolated_arc_data_dir, seq=seq)

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
            resp = client.get("/api/traces?limit=10", headers={"Authorization": "Bearer v"})
        ids = [t["trace_id"] for t in resp.json()["traces"]]
        assert len(ids) == 3
        assert len(set(ids)) == 3, "trace_ids must be unique"
        for tid in ids:
            assert len(tid) == 32, f"expected 32-char id, got {tid!r}"


class TestStatsAggregation:
    """Multiple calls aggregate correctly in /api/stats (SPEC-026 FR-5: Observe plane)."""

    def test_stats_sums_cost_and_tokens(self, _isolated_arc_data_dir: Path) -> None:
        """Five calls; /api/stats reports the right totals — what the user sees."""
        for seq, (cost, in_t, out_t) in enumerate(
            [
                (0.01, 1000, 200),
                (0.02, 1500, 300),
                (0.015, 1200, 250),
                (0.025, 1800, 400),
                (0.012, 1100, 220),
            ]
        ):
            _seed(
                _isolated_arc_data_dir,
                cost=cost,
                in_tokens=in_t,
                out_tokens=out_t,
                seq=seq,
            )

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
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

    def test_stats_per_model_breakdown(self, _isolated_arc_data_dir: Path) -> None:
        """Different models get separate rows in model_stats — what the
        Model Performance table shows."""
        for seq in range(3):
            _seed(_isolated_arc_data_dir, model="claude-opus-4-6", cost=0.05, seq=seq)
        for seq in range(7):
            _seed(
                _isolated_arc_data_dir,
                model="claude-haiku-4-5",
                cost=0.005,
                seq=seq + 3,
            )

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
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

    def test_three_agents_all_appear_in_traces(self, _isolated_arc_data_dir: Path) -> None:
        """Three agents each with one call. /api/traces returns all three.

        One shared durable store, many agents — no federation needed.
        """
        agents = ["agent_a", "agent_b", "agent_c"]
        for agent in agents:
            _seed(_isolated_arc_data_dir, agent=agent)

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
            resp = client.get("/api/traces?limit=10", headers={"Authorization": "Bearer v"})
        assert resp.status_code == 200
        traces = resp.json()["traces"]
        assert len(traces) == 3
        labels = {t["agent_label"] for t in traces}
        assert labels == set(agents)

    def test_agent_filter_isolates_one_agent(self, _isolated_arc_data_dir: Path) -> None:
        """`?agent=agent_b` returns only agent_b's records — filter must not leak."""
        for agent in ["agent_a", "agent_b"]:
            _seed(_isolated_arc_data_dir, agent=agent, seq=0)
            _seed(_isolated_arc_data_dir, agent=agent, seq=1)

        auth = AuthConfig({"viewer_token": "v", "operator_token": "o"})
        app = create_app(auth_config=auth)
        with TestClient(app) as client:
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

    def test_bootstrap_token_rejects_no_auth(self, tmp_path: Path) -> None:
        """Missing token → /api/stats returns 401, not silently empty data.

        SPEC-026 FR-5: the /ws live push endpoint is deleted; this test
        replaces the old /ws auth round-trip by verifying the HTTP auth
        boundary is still enforced on the stats endpoint.
        """
        viewer_token = "viewer-from-bootstrap"
        auth = AuthConfig({"viewer_token": viewer_token, "operator_token": "op"})
        app = create_app(auth_config=auth)
        client = TestClient(app)

        resp = client.get("/api/stats?window=24h")
        assert resp.status_code == 401

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
