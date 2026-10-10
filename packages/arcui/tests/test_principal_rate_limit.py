"""SPEC-087 T-1281: per-principal rate limit (COMP-005, REQ-576).

``PrincipalRateLimiter(rate_per_minute, clock)`` with an injected monotonic
clock (the clock is the external boundary; nothing sleeps). Only behaviour
both a fixed 60 s window and a token bucket agree on is pinned: N calls at one
instant pass, call N+1 is refused, a full minute later calls pass again, and
one key never spends another key's budget.

The middleware tests swap ``app.state.principal_rate_limiter`` for one on the
fake clock (the documented app-state seam) and go through the real
``create_app`` / ``AuthMiddleware`` / ``PrincipalStore``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust.principals import Scope
from arctrust.users import OPERATOR
from packages.arcui.tests.principal_fixtures import (
    OPERATOR_TOKEN,
    PrincipalApp,
    PrincipalRig,
    agent_did,
    build_principal_app,
    issue_principal,
    principal_rig,
    write_team,
)
from packages.arcui.tests.user_authority import user_store_factory
from starlette.testclient import TestClient

OLIVIA = agent_did()
INBOX = "/api/agents/olivia/inbox"
INBOX_PATTERN = "/api/agents/{id}/inbox"
APP_A = "did:arc:default:app/aaaaaaaa"
APP_B = "did:arc:default:app/bbbbbbbb"
RATE = 3


class FakeClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- PrincipalRateLimiter unit -----------------------------------------------------


class TestPrincipalRateLimiter:
    def _limiter(self, clock: FakeClock, rate: int = RATE) -> Any:
        from arcui.principals import PrincipalRateLimiter

        return PrincipalRateLimiter(rate, clock=clock)

    def test_first_n_calls_in_an_instant_pass_and_call_n_plus_one_is_refused(self) -> None:
        limiter = self._limiter(FakeClock())
        assert [limiter.allow(APP_A) for _ in range(RATE + 1)] == [True] * RATE + [False]

    def test_refused_while_inside_the_minute(self) -> None:
        clock = FakeClock()
        limiter = self._limiter(clock)
        for _ in range(RATE):
            limiter.allow(APP_A)
        clock.advance(1)
        assert limiter.allow(APP_A) is False

    def test_allowed_again_after_a_full_minute(self) -> None:
        clock = FakeClock()
        limiter = self._limiter(clock)
        for _ in range(RATE + 1):
            limiter.allow(APP_A)
        clock.advance(60.001)
        assert [limiter.allow(APP_A) for _ in range(RATE)] == [True] * RATE

    def test_refused_calls_do_not_extend_the_lockout(self) -> None:
        """Hammering while limited must not push the reset further out."""
        clock = FakeClock()
        limiter = self._limiter(clock)
        for _ in range(RATE):
            limiter.allow(APP_A)
        for _ in range(59):
            clock.advance(1)
            assert limiter.allow(APP_A) is False
        clock.advance(1.001)
        assert limiter.allow(APP_A) is True

    def test_one_principal_never_spends_anothers_budget(self) -> None:
        limiter = self._limiter(FakeClock())
        for _ in range(RATE + 5):
            limiter.allow(APP_A)
        assert [limiter.allow(APP_B) for _ in range(RATE)] == [True] * RATE
        assert limiter.allow(APP_B) is False

    def test_rate_of_one(self) -> None:
        clock = FakeClock()
        limiter = self._limiter(clock, rate=1)
        assert limiter.allow(APP_A) is True
        assert limiter.allow(APP_A) is False
        clock.advance(60.001)
        assert limiter.allow(APP_A) is True

    @pytest.mark.parametrize("rate", [0, -1])
    def test_non_positive_rate_refused_at_construction(self, rate: int) -> None:
        from arcui.principals import PrincipalRateLimiter

        with pytest.raises(ValueError):
            PrincipalRateLimiter(rate, clock=FakeClock())

    def test_config_default_rate_is_600_per_minute(self) -> None:
        from arcui.principals import PrincipalsConfig

        assert PrincipalsConfig(tier="personal").rate_per_minute == 600

    @pytest.mark.parametrize("rate", [0, -5])
    def test_config_refuses_non_positive_rate(self, rate: int) -> None:
        from arcui.principals import PrincipalsConfig
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            PrincipalsConfig(tier="personal", rate_per_minute=rate)


# --- through the middleware ----------------------------------------------------------


@pytest.fixture
def users(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    return user_store_factory(tmp_path / "accounts")


@pytest.fixture
def rig(tmp_path: Path) -> PrincipalRig:
    return principal_rig(tmp_path / "authority")


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def built(tmp_path: Path, rig: PrincipalRig, users: Any, clock: FakeClock) -> PrincipalApp:
    from arcui.principals import PrincipalRateLimiter

    built = build_principal_app(
        tmp_path,
        principal_store_factory=rig.factory,
        users=users,
        team_root=write_team(tmp_path / "team", {"olivia": OLIVIA}),
        tier="personal",
        rate_per_minute=RATE,
    )
    assert built.app.state.principal_rate_limiter is not None
    built.app.state.principal_rate_limiter = PrincipalRateLimiter(RATE, clock=clock)
    built.add_probe(INBOX_PATTERN)
    return built


def _client(built: PrincipalApp) -> TestClient:
    return TestClient(built.app, raise_server_exceptions=False)


def _issue(rig: PrincipalRig) -> Any:
    return issue_principal(rig.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})


class TestRateLimitInMiddleware:
    def test_request_n_plus_one_is_429_with_one_audit_row(
        self, built: PrincipalApp, rig: PrincipalRig
    ) -> None:
        app = _issue(rig)
        client = _client(built)
        statuses = [
            client.post(INBOX, json={}, headers=app.bearer_headers()).status_code
            for _ in range(RATE + 1)
        ]
        assert statuses == [200] * RATE + [429]
        assert len(built.probes) == RATE
        assert len(built.worm.denied()) == 1

    def test_allowed_again_after_the_window_resets(
        self, built: PrincipalApp, rig: PrincipalRig, clock: FakeClock
    ) -> None:
        app = _issue(rig)
        client = _client(built)
        for _ in range(RATE + 1):
            client.post(INBOX, json={}, headers=app.bearer_headers())
        clock.advance(60.001)
        resp = client.post(INBOX, json={}, headers=app.bearer_headers())
        assert resp.status_code == 200, resp.text
        assert len(built.probes) == RATE + 1

    def test_one_principals_limit_never_affects_another(
        self, built: PrincipalApp, rig: PrincipalRig
    ) -> None:
        limited, other = _issue(rig), _issue(rig)
        client = _client(built)
        for _ in range(RATE + 2):
            client.post(INBOX, json={}, headers=limited.bearer_headers())
        statuses = [
            client.post(INBOX, json={}, headers=other.bearer_headers()).status_code
            for _ in range(RATE)
        ]
        assert statuses == [200] * RATE

    def test_a_limited_principal_never_affects_sessions_or_the_operator_token(
        self, built: PrincipalApp, rig: PrincipalRig
    ) -> None:
        app = _issue(rig)
        client = _client(built)
        for _ in range(RATE + 2):
            client.post(INBOX, json={}, headers=app.bearer_headers())
        token, _ = built.sign_in("boss@example.com", (OPERATOR,))
        for credential in (token, OPERATOR_TOKEN):
            statuses = {
                client.post(
                    INBOX, json={}, headers={"Authorization": f"Bearer {credential}"}
                ).status_code
                for _ in range(RATE * 3)
            }
            assert statuses == {200}

    def test_limit_is_keyed_by_principal_not_by_client_address(
        self, built: PrincipalApp, rig: PrincipalRig
    ) -> None:
        """A spoofed client address never buys the same principal a fresh budget."""
        app = _issue(rig)
        client = _client(built)
        for _ in range(RATE):
            assert client.post(INBOX, json={}, headers=app.bearer_headers()).status_code == 200
        resp = client.post(
            INBOX, json={}, headers=app.bearer_headers(**{"X-Forwarded-For": "1.2.3.4"})
        )
        assert resp.status_code == 429
