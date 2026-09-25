"""Hosted setup keeps the signed machine grant separate from browser proof."""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from arcui.auth import AuthConfig
from arcui.routes.hosted_setup import HostedBusyError, _bounded_call
from arcui.server import create_app


class Claims:
    def __init__(self) -> None:
        self.grants: list[dict[str, object]] = []
        self.claims: list[tuple[str, str]] = []

    def status(self) -> str:
        return "awaiting_setup" if self.grants else "awaiting_grant"

    def signed_challenge(self):
        return {"facts": {"nonce": "n" * 32}, "signature": "machine-signed"}

    def install_grant(self, grant: dict[str, object]) -> None:
        self.grants.append(grant)

    def claim(self, secret: str, password: str):
        self.claims.append((secret, password))
        return SimpleNamespace(email="customer@example.com", did="did:arc:acme:user/first")


def _client() -> tuple[TestClient, Claims]:
    claims = Claims()
    app = create_app(
        auth_config=AuthConfig({"viewer_token": "v" * 64, "operator_token": "o" * 64}),
        hosted=True,
        hosted_claim=claims,
        hosted_origin="https://first.example.com",
    )
    return TestClient(app), claims


def test_machine_grant_and_browser_claim_are_separate() -> None:
    client, claims = _client()
    assert client.get("/api/setup/challenge").json()["facts"]["nonce"] == "n" * 32
    assert client.get("/api/setup/status").json()["status"] == "awaiting_grant"
    response = client.post("/api/setup/grant", json={"facts": {}, "signature": "signed"})
    assert response.status_code == 202
    assert client.get("/api/setup/status").json()["status"] == "awaiting_setup"
    response = client.post(
        "/api/setup/claim",
        headers={"Origin": "https://first.example.com"},
        json={"customer_secret": "s" * 43, "password": "correct-horse-battery"},
    )
    assert response.status_code == 200
    assert claims.grants == [{"facts": {}, "signature": "signed"}]
    assert claims.claims == [("s" * 43, "correct-horse-battery")]


def test_cross_origin_browser_claim_refused() -> None:
    client, claims = _client()
    response = client.post(
        "/api/setup/claim",
        headers={"Origin": "https://attacker.example"},
        json={"customer_secret": "s" * 43, "password": "correct-horse-battery"},
    )
    assert response.status_code == 403
    assert claims.claims == []


def test_claim_is_absent_when_hosted_authority_is_not_supplied() -> None:
    app = create_app(
        auth_config=AuthConfig({"viewer_token": "v" * 64, "operator_token": "o" * 64}),
        hosted=True,
    )
    client = TestClient(app)
    assert (
        client.post(
            "/api/setup/claim",
            headers={"Origin": "https://first.example.com"},
            json={"customer_secret": "s" * 43, "password": "correct-horse-battery"},
        ).status_code
        == 503
    )


def test_rekey_endpoint_requires_separate_service_and_bounds_delivery() -> None:
    client, _claims = _client()
    assert client.get("/api/setup/rekey-intent").status_code == 503
    assert client.post("/api/setup/rekey", json={"facts": {}}).status_code == 503

    class Rekey:
        def __init__(self) -> None:
            self.installed: list[dict[str, object]] = []

        def signed_intent(self):
            return {"facts": {"previous_head_digest": "d" * 64}, "signature": "new-key"}

        def install_rekey(self, envelope):
            self.installed.append(envelope)

    rekey = Rekey()
    client.app.state.hosted_rekey = rekey
    assert (
        client.get("/api/setup/rekey-intent").json()["facts"]["previous_head_digest"] == "d" * 64
    )
    assert (
        client.post("/api/setup/rekey", json={"facts": {}, "signature": "issuer"}).status_code
        == 202
    )
    assert rekey.installed == [{"facts": {}, "signature": "issuer"}]


@pytest.mark.asyncio
async def test_cancelled_requests_keep_worker_slots_until_real_completion() -> None:
    app = create_app(
        auth_config=AuthConfig({"viewer_token": "v" * 64, "operator_token": "o" * 64}),
        hosted=True,
    )
    request = SimpleNamespace(app=app)
    block = threading.Event()
    started = threading.Event()
    count = 0

    def slow_claim() -> None:
        nonlocal count
        count += 1
        if count == 2:
            started.set()
        block.wait(2)

    first = asyncio.create_task(_bounded_call(request, slow_claim))
    second = asyncio.create_task(_bounded_call(request, slow_claim))
    assert await asyncio.to_thread(started.wait, 1)
    first.cancel()
    second.cancel()
    for task in (first, second):
        with pytest.raises(asyncio.CancelledError):
            await task
    with pytest.raises(HostedBusyError):
        await _bounded_call(request, slow_claim)
    assert count == 2
    block.set()
    await asyncio.sleep(0.05)
    await _bounded_call(request, slow_claim)
    assert count == 3
    app.state.hosted_claim_executor.shutdown(wait=True)
