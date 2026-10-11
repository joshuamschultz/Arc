"""SPEC-088: pre-authentication abuse on the app-principal path (LLM10, ASI08).

An unauthenticated stranger who sends ``X-Arc-Principal`` or an ``arcp_``
bearer must not be able to make arcui buffer an unbounded body, open the
principal authority (Vault / sealed store), or write fsync'd audit rows at
will. Two cheap gates run first, before any of that:

* a per-client-IP throttle: over the limit is 429 ``rate_limited``, no audit
  row, no store load, and at most one warning log line per window per address;
* a body cap: over it is 413, no audit row, no store load, and the stream is
  not drained (a lying or absent ``Content-Length`` cannot get past it).

Everything goes through the real ``create_app`` / ``AuthMiddleware`` and a real
``PrincipalStore``. The store factory is wrapped by a call counter, and the
dashboard audit WORM is the fixtures' spy.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from arctrust.principals import PrincipalStore, Scope
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
from starlette.testclient import TestClient

OLIVIA = agent_did()
INBOX = "/api/agents/olivia/inbox"
INBOX_PATTERN = "/api/agents/{id}/inbox"
RATE = 3
ATTACKER = ("203.0.113.7", 40000)
BYSTANDER = ("198.51.100.9", 40000)
PRE_AUTH_LOG = "principal.pre_auth_rate_limited"


@dataclass
class CountingFactory:
    """The real store factory, counting every authority open."""

    inner: Callable[[], PrincipalStore]
    calls: int = 0

    def __call__(self) -> PrincipalStore:
        self.calls += 1
        return self.inner()


@dataclass
class Rig:
    built: PrincipalApp
    factory: CountingFactory
    store: PrincipalRig

    def client(self, address: tuple[str, int] = ATTACKER) -> TestClient:
        return TestClient(self.built.app, raise_server_exceptions=False, client=address)

    def rows(self) -> int:
        return len(self.built.worm.written)


@pytest.fixture
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Rig:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))
    store = principal_rig(tmp_path / "authority")
    factory = CountingFactory(store.factory)
    built = build_principal_app(
        tmp_path,
        principal_store_factory=factory,
        users=user_store_factory(tmp_path / "accounts"),
        team_root=write_team(tmp_path / "team", {"olivia": OLIVIA}),
        tier="personal",
        pre_auth_rate_per_minute=RATE,
    )
    built.add_probe(INBOX_PATTERN)
    return Rig(built=built, factory=factory, store=store)


def _forged_signed_headers(tmp_path: Path, body: bytes = b"") -> dict[str, str]:
    """A well-formed signed envelope from a key the app's authority never issued."""
    stranger = principal_rig(tmp_path / "stranger-authority")
    forged = issue_principal(stranger.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
    return signed_headers(forged, method="POST", path=INBOX, body=body)


def _spend_budget(rig: Rig, headers: dict[str, str]) -> None:
    """Use the address's whole budget; each of these reaches verification (401)."""
    client = rig.client()
    for _ in range(RATE):
        assert client.post(INBOX, headers=headers).status_code == 401


# --- 1. per-client-IP throttle -----------------------------------------------------


class TestPreAuthThrottle:
    def test_signed_flood_from_one_ip_gets_429_with_no_row_and_no_store_load(
        self, tmp_path: Path, rig: Rig
    ) -> None:
        headers = _forged_signed_headers(tmp_path)
        _spend_budget(rig, headers)
        assert rig.factory.calls == RATE  # the counter sees real loads
        rows, loads = rig.rows(), rig.factory.calls

        client = rig.client()
        for _ in range(20):
            response = client.post(INBOX, headers=headers)
            assert response.status_code == 429
            assert response.json() == {"error": "rate_limited"}

        assert rig.rows() == rows
        assert rig.factory.calls == loads

    def test_bearer_flood_is_throttled_pre_auth(self, rig: Rig) -> None:
        headers = {"Authorization": "Bearer arcp_notreal.notasecret"}
        _spend_budget(rig, headers)
        rows, loads = rig.rows(), rig.factory.calls

        client = rig.client()
        statuses = {client.post(INBOX, headers=headers).status_code for _ in range(10)}

        assert statuses == {429}
        assert rig.rows() == rows
        assert rig.factory.calls == loads

    def test_another_ip_is_unaffected(self, tmp_path: Path, rig: Rig) -> None:
        headers = _forged_signed_headers(tmp_path)
        _spend_budget(rig, headers)
        assert rig.client().post(INBOX, headers=headers).status_code == 429

        valid = issue_principal(rig.store.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        response = rig.client(BYSTANDER).post(INBOX, headers=valid.bearer_headers())

        assert response.status_code == 200, response.text
        assert rig.built.probes[-1]["principal_did"] == valid.did

    def test_x_forwarded_for_does_not_move_the_bucket(self, tmp_path: Path, rig: Rig) -> None:
        """No trusted-proxy setting exists, so a spoofed header buys no fresh budget."""
        headers = _forged_signed_headers(tmp_path)
        _spend_budget(rig, headers)

        spoofed = {**headers, "X-Forwarded-For": BYSTANDER[0]}
        assert rig.client().post(INBOX, headers=spoofed).status_code == 429

    def test_flood_logs_one_warning_per_window_per_ip(
        self, tmp_path: Path, rig: Rig, caplog: pytest.LogCaptureFixture
    ) -> None:
        headers = _forged_signed_headers(tmp_path)
        _spend_budget(rig, headers)

        with caplog.at_level(logging.WARNING, logger="arcui.principals"):
            client = rig.client()
            for _ in range(25):
                client.post(INBOX, headers=headers)

        warnings = [r for r in caplog.records if PRE_AUTH_LOG in r.getMessage()]
        assert len(warnings) == 1
        assert ATTACKER[0] in warnings[0].getMessage()


# --- 2. body cap -------------------------------------------------------------------


def _oversized() -> bytes:
    from arcui.principals import APP_BODY_CAP_BYTES

    return b"x" * (APP_BODY_CAP_BYTES + 1)


class TestPreAuthBodyCap:
    def test_oversized_signed_body_is_413_with_no_row_and_no_store_load(
        self, tmp_path: Path, rig: Rig
    ) -> None:
        body = _oversized()
        response = rig.client().post(
            INBOX, content=body, headers=_forged_signed_headers(tmp_path, body)
        )

        assert response.status_code == 413
        assert response.json() == {"error": "body_too_large"}
        assert rig.rows() == 0
        assert rig.factory.calls == 0

    def test_oversized_bearer_body_is_413_with_no_row_and_no_store_load(self, rig: Rig) -> None:
        response = rig.client().post(
            INBOX,
            content=_oversized(),
            headers={"Authorization": "Bearer arcp_notreal.notasecret"},
        )

        assert response.status_code == 413
        assert rig.rows() == 0
        assert rig.factory.calls == 0

    def test_a_valid_app_over_the_cap_is_refused_before_verification(self, rig: Rig) -> None:
        valid = issue_principal(rig.store.open(), scopes={Scope.INBOX_SEND: (OLIVIA,)})
        body = _oversized()
        headers = signed_headers(valid, method="POST", path=INBOX, body=body)

        response = rig.client().post(INBOX, content=body, headers=headers)

        assert response.status_code == 413
        assert rig.factory.calls == 0
        assert rig.built.probes == []

    @pytest.mark.parametrize(
        "content_length",
        [None, b"10"],
        ids=["absent-content-length", "lying-content-length"],
    )
    def test_streamed_body_is_not_drained_past_the_cap(
        self, rig: Rig, content_length: bytes | None
    ) -> None:
        from arcui.principals import APP_BODY_CAP_BYTES

        chunk, total = 16 * 1024, 8 * 1024 * 1024
        headers = [(b"x-arc-principal", b"did:arc:default:app/forged")]
        if content_length is not None:
            headers.append((b"content-length", content_length))

        status, delivered = asyncio.run(_drive(rig.built.app, headers, chunk, total))

        assert status == 413
        assert delivered <= APP_BODY_CAP_BYTES + chunk
        assert rig.rows() == 0
        assert rig.factory.calls == 0


async def _drive(
    app: Any, headers: list[tuple[bytes, bytes]], chunk: int, total: int
) -> tuple[int, int]:
    """Send ``total`` bytes in ``chunk``s straight over ASGI; count what was pulled."""
    sent = 0
    status = 0

    async def receive() -> dict[str, Any]:
        nonlocal sent
        if sent >= total:
            return {"type": "http.disconnect"}
        sent += chunk
        return {"type": "http.request", "body": b"x" * chunk, "more_body": sent < total}

    async def send(message: dict[str, Any]) -> None:
        nonlocal status
        if message["type"] == "http.response.start":
            status = message["status"]

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": INBOX,
        "raw_path": INBOX.encode(),
        "root_path": "",
        "query_string": b"",
        "headers": [(b"host", b"testserver"), *headers],
        "client": ATTACKER,
        "server": ("testserver", 80),
        "state": {},
    }
    await app(scope, receive, send)
    return status, sent
