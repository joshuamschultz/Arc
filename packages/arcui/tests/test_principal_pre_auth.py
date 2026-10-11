"""SPEC-088: pre-authentication hardening on the app-principal path.

Before any principal-authority load or audit write, a request presenting an app
credential (``X-Arc-Principal`` or an ``arcp_`` bearer) passes two cheap gates:

1. a per-client-IP throttle (``[ui.principals] pre_auth_rate_per_minute``), and
2. a body cap (:data:`arcui.principals.APP_BODY_CAP_BYTES`) read through
   :func:`arcui.request_body.read_capped_body`.

This file pins the contract and the happy path: the config field, the limiter's
shape, the cap covering every app route's own cap, and a request under both
gates still authenticating with the route reading the full body. The attacker's
view (floods, oversized and lying bodies) lives in
``security/test_principal_pre_auth_abuse.py``.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from arctrust.principals import Scope
from packages.arcui.tests.principal_fixtures import (
    PrincipalApp,
    PrincipalRig,
    agent_did,
    build_principal_app,
    issue_principal,
    principal_rig,
    signed_headers,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from pydantic import ValidationError
from starlette.requests import Request
from starlette.testclient import TestClient

OLIVIA = agent_did()
INBOX = "/api/agents/olivia/inbox"
INBOX_PATTERN = "/api/agents/{id}/inbox"


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def rig(tmp_path: Path) -> PrincipalRig:
    return principal_rig(tmp_path / "authority")


@pytest.fixture
def team(tmp_path: Path) -> Path:
    return write_team(tmp_path / "team", {"olivia": OLIVIA})


def _app(tmp_path: Path, rig: PrincipalRig, users: Any, team: Path, **kw: Any) -> PrincipalApp:
    kw.setdefault("tier", "personal")
    built = build_principal_app(
        tmp_path, principal_store_factory=rig.factory, users=users, team_root=team, **kw
    )
    built.add_probe(INBOX_PATTERN)
    return built


# --- config ----------------------------------------------------------------------


class TestPreAuthConfig:
    def test_default_pre_auth_rate_covers_one_app_at_its_full_rate(self) -> None:
        from arcui.principals import PrincipalsConfig

        config = PrincipalsConfig(tier="personal")
        assert config.pre_auth_rate_per_minute == 1200
        assert config.pre_auth_rate_per_minute >= config.rate_per_minute

    def test_pre_auth_rate_must_be_positive(self) -> None:
        from arcui.principals import PrincipalsConfig

        with pytest.raises(ValidationError):
            PrincipalsConfig(tier="personal", pre_auth_rate_per_minute=0)

    def test_create_app_builds_the_pre_auth_limiter_from_config(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        from arcui.principals import PrincipalRateLimiter

        built = _app(tmp_path, rig, users, team, pre_auth_rate_per_minute=2)
        limiter = built.app.state.principal_pre_auth_limiter
        assert isinstance(limiter, PrincipalRateLimiter)
        assert limiter is not built.app.state.principal_rate_limiter
        assert [limiter.allow("10.0.0.1") for _ in range(3)] == [True, True, False]


# --- the body cap covers every app route -------------------------------------------


class TestAppBodyCap:
    def test_cap_covers_the_largest_app_route_cap(self) -> None:
        from arcui.principals import APP_BODY_CAP_BYTES
        from arcui.routes import runs_intake, tasks, workflows

        route_caps = (
            runs_intake.MAX_BODY_BYTES,
            tasks.MAX_BODY_BYTES,
            workflows.PAYLOAD_CAP_BYTES,
            workflows._GATE_BODY_CAP_BYTES,
        )
        assert max(route_caps) == APP_BODY_CAP_BYTES


# --- limiter: refusal ordinal + bounded keys ---------------------------------------


class TestLimiterShape:
    def test_excess_counts_calls_over_budget_in_the_window(self) -> None:
        from arcui.principals import PrincipalRateLimiter

        limiter = PrincipalRateLimiter(2, clock=FakeClock())
        assert [limiter.excess("ip") for _ in range(5)] == [0, 0, 1, 2, 3]

    def test_excess_resets_with_the_window(self) -> None:
        from arcui.principals import PrincipalRateLimiter

        clock = FakeClock()
        limiter = PrincipalRateLimiter(1, clock=clock)
        assert [limiter.excess("ip") for _ in range(2)] == [0, 1]
        clock.now += 60.0
        assert [limiter.excess("ip") for _ in range(2)] == [0, 1]

    def test_key_map_is_bounded(self) -> None:
        """Untrusted keys (client addresses) cannot grow the map without bound."""
        from arcui.principals import PrincipalRateLimiter

        limiter = PrincipalRateLimiter(5, clock=FakeClock(), max_keys=3)
        for n in range(50):
            limiter.allow(f"10.0.0.{n}")
        assert limiter.tracked_keys() <= 3

    def test_expired_windows_are_dropped_before_live_ones(self) -> None:
        from arcui.principals import PrincipalRateLimiter

        clock = FakeClock()
        limiter = PrincipalRateLimiter(1, clock=clock, max_keys=2)
        limiter.allow("old")
        clock.now += 61.0
        limiter.allow("live")
        assert limiter.allow("live") is False
        limiter.allow("new")  # map full: the expired "old" window goes, "live" stays
        assert limiter.allow("live") is False

    def test_max_keys_must_be_positive(self) -> None:
        from arcui.principals import PrincipalRateLimiter

        with pytest.raises(ValueError, match="max_keys"):
            PrincipalRateLimiter(1, max_keys=0)


# --- read_capped_body keeps the body readable --------------------------------------


def _request(body: bytes) -> Request:
    chunks = [body[i : i + 7] for i in range(0, len(body), 7)] or [b""]
    messages = [
        {"type": "http.request", "body": chunk, "more_body": i < len(chunks) - 1}
        for i, chunk in enumerate(chunks)
    ]

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    scope = {"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b""}
    return Request(scope, receive)


class TestCappedBodyIsCached:
    def test_body_is_readable_again_after_a_capped_read(self) -> None:
        from arcui.request_body import read_capped_body

        async def run() -> tuple[bytes | None, bytes]:
            request = _request(b"0123456789abcdefghij")
            first = await read_capped_body(request, 100)
            return first, await request.body()

        first, again = asyncio.run(run())
        assert first == again == b"0123456789abcdefghij"


# --- happy path through the real middleware ----------------------------------------


class TestUnderBothGates:
    def test_signed_request_near_the_cap_authenticates_and_route_reads_full_body(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        from arcui.principals import APP_BODY_CAP_BYTES

        built = _app(tmp_path, rig, users, team)
        app = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        body = json.dumps({"body": "x" * (APP_BODY_CAP_BYTES - 64)}).encode()
        assert 100 * 1024 < len(body) <= APP_BODY_CAP_BYTES
        headers = signed_headers(app, method="POST", path=INBOX, body=body)

        response = TestClient(built.app).post(INBOX, content=body, headers=headers)

        assert response.status_code == 200, response.text
        assert built.probes[-1]["principal_did"] == app.did
        assert built.probes[-1]["body"] == body.decode()

    def test_bearer_request_with_body_authenticates_and_route_reads_full_body(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        built = _app(tmp_path, rig, users, team)
        app = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        body = json.dumps({"body": "hello"}).encode()

        response = TestClient(built.app).post(INBOX, content=body, headers=app.bearer_headers())

        assert response.status_code == 200, response.text
        assert built.probes[-1]["principal_did"] == app.did
        assert built.probes[-1]["body"] == body.decode()

    def test_a_valid_app_is_still_throttled_per_principal_after_pre_auth(
        self, tmp_path: Path, rig: PrincipalRig, users: Any, team: Path
    ) -> None:
        """Step 3 (per-principal rate, audited) is unchanged behind the pre-auth gate."""
        built = _app(tmp_path, rig, users, team, rate_per_minute=1)
        app = issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        client = TestClient(built.app)

        assert client.post(INBOX, headers=app.bearer_headers()).status_code == 200
        refused = client.post(INBOX, headers=app.bearer_headers())

        assert refused.status_code == 429
        assert len(built.worm.denied()) == 1
