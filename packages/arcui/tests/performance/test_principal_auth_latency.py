"""Principal authentication adds at most 50 ms at p95 (SPEC-087 T-1292, REQ-577).

1,000 scoped bearer requests and 1,000 operator-session requests go to the same
cheap route through the real ``create_app`` build and its real
``AuthMiddleware``. The principal path is the production one end to end: the
authority is a real ``PrincipalStore`` over the file-journal test anchor,
reopened per request by the factory, the bearer digest is verified, the
per-principal rate limiter runs, the route table is matched, the path agent is
resolved through the real ``team/`` roster and checked against the scope, and
``last_used_at`` is touched. The handler is a probe mounted ahead of the inbox
handler, so both sides pay the same handler cost and the difference is the
authentication and authorization path alone.

The two kinds of request are interleaved so drift on a busy machine (thermal,
GC, a neighbour process) lands on both samples rather than on one.

The rate limit is raised for this test only: 1,000 requests in a few seconds
would trip the 600/min default, and the budget is about auth cost, not the
limiter's refusal.
"""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from arctrust.principals import Scope
from arctrust.users import OPERATOR
from packages.arcui.tests.principal_fixtures import (
    agent_did,
    build_principal_app,
    issue_principal,
    principal_rig,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

logger = logging.getLogger(__name__)

REQUESTS = 1_000
WARMUP = 20
P95_BUDGET_MS = 50.0
INBOX_PATTERN = "/api/agents/{id}/inbox"
INBOX_OLIVIA = "/api/agents/olivia/inbox"
BODY = b'{"subject":"latency","body":"probe"}'


def p95(samples_ms: list[float]) -> float:
    """95th percentile (inclusive method: never extrapolates past the slowest sample)."""
    return statistics.quantiles(samples_ms, n=100, method="inclusive")[94]


def _timed(send: Callable[[], Any]) -> float:
    start = time.perf_counter()
    response = send()
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    assert response.status_code == 200, response.text
    return elapsed_ms


def test_p95_helper_matches_a_known_distribution() -> None:
    assert p95([float(n) for n in range(1, 101)]) == pytest.approx(95.05)


def test_principal_auth_adds_at_most_50ms_at_p95(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    record_property: Callable[[str, object], None],
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    olivia = agent_did()
    rig = principal_rig(tmp_path / "authority")
    built = build_principal_app(
        tmp_path,
        principal_store_factory=rig.factory,
        users=user_store_factory(tmp_path / "accounts"),
        team_root=write_team(tmp_path / "team", {"olivia": olivia}),
        tier="personal",
        rate_per_minute=1_000_000,
    )
    built.add_probe(INBOX_PATTERN)
    issued = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (olivia,)})
    session_token, _ = built.sign_in("boss@example.com", (OPERATOR,))

    client = TestClient(built.app, raise_server_exceptions=False)
    principal_headers = issued.bearer_headers()
    session_headers = {"Authorization": f"Bearer {session_token}"}

    def as_principal() -> Any:
        return client.post(INBOX_OLIVIA, content=BODY, headers=principal_headers)

    def as_session() -> Any:
        return client.post(INBOX_OLIVIA, content=BODY, headers=session_headers)

    for _ in range(WARMUP):
        _timed(as_principal)
        _timed(as_session)

    principal_ms: list[float] = []
    session_ms: list[float] = []
    for _ in range(REQUESTS):
        principal_ms.append(_timed(as_principal))
        session_ms.append(_timed(as_session))

    # Every principal request really took the principal path to the probe.
    principal_hits = [p for p in built.probes if p["principal_kind"] == "app"]
    assert len(principal_hits) == WARMUP + REQUESTS

    principal_p95 = p95(principal_ms)
    session_p95 = p95(session_ms)
    added = principal_p95 - session_p95
    measured = {
        "principal_p95_ms": round(principal_p95, 3),
        "session_p95_ms": round(session_p95, 3),
        "added_p95_ms": round(added, 3),
        "principal_median_ms": round(statistics.median(principal_ms), 3),
        "session_median_ms": round(statistics.median(session_ms), 3),
    }
    for key, value in measured.items():
        record_property(key, value)
    logger.warning("principal auth latency over %d requests each: %s", REQUESTS, measured)

    assert added <= P95_BUDGET_MS, (
        f"principal auth adds {added:.1f} ms at p95 (budget {P95_BUDGET_MS} ms): {measured}"
    )
