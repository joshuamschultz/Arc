"""P18-2 §8.3 — single refresher, persist-before-use, and the crash windows.

"Two processes" are two :class:`RenewalPlanner` instances with different owners and
separate in-process locks over one backend. Interleaving is forced with
``asyncio.Barrier``/``Event`` inside the backend or the fake provider, never sleeps.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from arctrust.audit import AuditEvent
from packages.arcagent.tests.custody_fakes import InterleavingBackend, make_cipher, once_per_task

from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credentials import (
    CredentialRenewalError,
    RefreshRequest,
    RenewalPlanner,
    RenewedCredential,
)
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcagent.extension.manifest import OAuthFlow
from arcagent.extension.oauth import refresh_access_token
from arcagent.extension.secrets import Secret
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore

FLOW = OAuthFlow(
    authorize_url="https://auth.example/authorize",
    token_url="https://auth.example/token",
    client_id_secret="app_key",
    client_secret_secret="app_secret",
    refresh_token_secret="refresh_token",
)
CONN = "blackarc"
ACTOR = "did:arc:operator:test"
START = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)

    def actions(self, action: str) -> list[AuditEvent]:
        return [event for event in self.events if event.action == action]


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class Provider:
    """A fake token endpoint. ``rotate`` makes every refresh token single-use."""

    def __init__(self, *, rotate: bool = True, lifetime: int = 3600) -> None:
        self.rotate = rotate
        self.lifetime = lifetime
        self.current = "r0"
        self.consumed: set[str] = set()
        self.calls = 0
        self.invalid_grants = 0
        self.issued = 0
        self.pause: asyncio.Event | None = None
        self.entered = asyncio.Event()
        self.rotated_before_pause = False

    async def __call__(self, request: RefreshRequest) -> RenewedCredential:
        self.calls += 1
        presented = request.refresh_token.reveal()
        assert request.client_secret.reveal() == "app-secret"
        if presented in self.consumed or presented != self.current:
            self.invalid_grants += 1
            raise CredentialRenewalError(error_code="invalid_grant", message="invalid_grant")
        self.issued += 1
        rotated = None
        if self.rotate:
            self.consumed.add(presented)
            rotated = f"r{self.issued}"
            self.current = rotated
        self.entered.set()
        if self.pause is not None:
            self.rotated_before_pause = True
            await self.pause.wait()
        return RenewedCredential(
            access_token=Secret(f"a{self.issued}"),
            expires_in=self.lifetime,
            refresh_token=Secret(rotated) if rotated else None,
        )


async def _fast_sleep(_seconds: float) -> None:
    await asyncio.sleep(0)


class World:
    def __init__(self) -> None:
        self.backend = InterleavingBackend()
        self.clock = Clock()
        self.sink = ListSink()
        self.state = ConnectionStateStore(self.backend)

    async def open(self) -> Any:
        return self.backend

    async def seed(self) -> None:
        rows = self.rows()
        await rows.put_fields(
            CONN,
            {"app_key": "app-key", "app_secret": "app-secret", "refresh_token": "r0"},
            actor_did=ACTOR,
        )
        await self.state.create(ConnectionRecord(connection=CONN, custody="arc"), actor_did=ACTOR)

    def rows(self) -> CredentialRowStore:
        return CredentialRowStore(self.backend, make_cipher(), clock=self.clock)

    def planner(
        self,
        owner: str,
        provider: Callable[[RefreshRequest], Awaitable[RenewedCredential]],
        **kwargs: Any,
    ) -> RenewalPlanner:
        return RenewalPlanner(
            rows=self.rows(),
            refresh=provider,
            health=StoreHealthReporter(self.open, sink=self.sink),
            owner_id=owner,
            state=self.state,
            sink=self.sink,
            clock=self.clock,
            sleep=_fast_sleep,
            **kwargs,
        )

    async def access(self) -> str:
        rows = self.rows()
        row = await rows.read(CONN)
        assert row is not None
        token = rows.open_access(row)
        assert token is not None
        return token.token.reveal()

    async def refresh_token(self) -> str:
        rows = self.rows()
        row = await rows.read(CONN)
        assert row is not None
        found = rows.open_field(row, "refresh_token")
        assert found is not None
        return found.reveal()


@pytest.fixture
async def world() -> World:
    built = World()
    await built.seed()
    return built


async def test_token_expiring_mid_sync_renews_once_across_two_processes(world: World) -> None:
    provider = Provider()
    first, second = world.planner("proc-a", provider), world.planner("proc-b", provider)
    barrier = asyncio.Barrier(2)
    world.backend.before_update_if = once_per_task(barrier.wait, collection=CREDENTIAL_COLLECTION)

    results = await asyncio.gather(
        first.ensure_fresh(CONN, flow=FLOW), second.ensure_fresh(CONN, flow=FLOW)
    )

    assert provider.calls == 1
    assert sorted(results) == [False, True]
    assert await world.access() == "a1"
    assert len(world.sink.actions("connection.credential.renewed")) == 1


async def test_rotating_refresh_token_persisted_before_use_and_old_never_reused(
    world: World,
) -> None:
    provider = Provider(rotate=True)
    first, second = world.planner("proc-a", provider), world.planner("proc-b", provider)
    for _ in range(5):
        barrier = asyncio.Barrier(2)
        world.backend.before_update_if = once_per_task(
            barrier.wait, collection=CREDENTIAL_COLLECTION
        )
        await asyncio.gather(
            first.ensure_fresh(CONN, flow=FLOW), second.ensure_fresh(CONN, flow=FLOW)
        )
        world.backend.before_update_if = None
        world.clock.advance(minutes=46)  # past 75 % of the hour

    assert provider.calls == 5
    assert provider.invalid_grants == 0
    assert await world.refresh_token() == provider.current == "r5"


async def test_rotated_token_is_committed_before_any_caller_sees_it(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = Provider(rotate=True)
    planner = world.planner("proc-a", provider)

    async def lost(*_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(planner.rows, "commit_renewal", lost)
    with pytest.raises(CredentialRenewalError) as caught:
        await planner.ensure_fresh(CONN, flow=FLOW)
    assert caught.value.error_code == "renewal_commit_lost"
    assert provider.calls == 1
    row = await world.rows().read(CONN)
    assert row is not None and row.access is None  # nothing a caller could have used
    assert row.lease is None  # released, the next holder is not blocked


async def test_crash_after_provider_rotate_before_persist_is_needs_you_not_a_loop(
    world: World,
) -> None:
    provider = Provider(rotate=True)
    provider.pause = asyncio.Event()
    doomed = asyncio.create_task(world.planner("proc-a", provider).ensure_fresh(CONN, flow=FLOW))
    await provider.entered.wait()
    assert provider.rotated_before_pause
    doomed.cancel()  # the process dies between the provider's answer and the commit
    with pytest.raises(asyncio.CancelledError):
        await doomed
    world.clock.advance(seconds=61)  # whatever lease it held has expired
    provider.pause = None

    restarted = world.planner("proc-b", provider)
    with pytest.raises(CredentialRenewalError) as caught:
        await restarted.ensure_fresh(CONN, flow=FLOW)
    assert caught.value.error_code == "invalid_grant"
    record = await world.state.get(CONN)
    assert record is not None
    assert (record.status, record.action) == ("needs_you", "reconnect")
    assert record.notice_seq == 1
    calls = provider.calls

    # Restart again: still needs_you, and the dead token is never presented again.
    again = world.planner("proc-c", provider)
    with pytest.raises(CredentialRenewalError):
        await again.ensure_fresh(CONN, flow=FLOW)
    with pytest.raises(CredentialRenewalError):
        await again.ensure_fresh(CONN, flow=FLOW, force=True)
    assert provider.calls == calls
    record = await world.state.get(CONN)
    assert record is not None and record.status == "needs_you" and record.notice_seq == 1

    # The operator reconnects: a new generation is worth exactly one provider call.
    provider.current = "fresh"
    await world.rows().put_fields(CONN, {"refresh_token": "fresh"}, actor_did=ACTOR)
    assert await again.ensure_fresh(CONN, flow=FLOW) is True
    assert provider.calls == calls + 1


async def test_crash_after_commit_before_metadata_renews_from_the_row(
    world: World, monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = Provider()
    planner = world.planner("proc-a", provider)

    async def crash(*_args: Any, **_kwargs: Any) -> bool:
        raise RuntimeError("process died writing the mirror")

    monkeypatch.setattr(world.state, "record_credential_metadata", crash)
    assert await planner.ensure_fresh(CONN, flow=FLOW) is True
    record = await world.state.get(CONN)
    assert record is not None and record.credential_expires_at is None  # stale mirror

    assert await world.planner("proc-b", provider).ensure_fresh(CONN, flow=FLOW) is False
    assert provider.calls == 1


async def test_stalled_holder_loses_the_lease_and_cannot_commit(world: World) -> None:
    stalled = Provider(rotate=False)
    stalled.pause = asyncio.Event()
    holder_a = asyncio.create_task(world.planner("proc-a", stalled).ensure_fresh(CONN, flow=FLOW))
    await stalled.entered.wait()
    world.clock.advance(seconds=61)  # A stalls past its TTL

    healthy = Provider(rotate=False)
    healthy.issued = 10
    assert await world.planner("proc-b", healthy).ensure_fresh(CONN, flow=FLOW) is True
    stalled.pause.set()
    with pytest.raises(CredentialRenewalError) as caught:
        await holder_a
    assert caught.value.error_code == "renewal_commit_lost"
    assert await world.access() == "a11"


async def test_waiter_sees_fresh_token_and_skips_the_provider(world: World) -> None:
    provider = Provider()
    provider.pause = asyncio.Event()
    holder = asyncio.create_task(world.planner("proc-a", provider).ensure_fresh(CONN, flow=FLOW))
    await provider.entered.wait()

    waiting = asyncio.Event()

    async def watched_sleep(_seconds: float) -> None:
        waiting.set()
        await asyncio.sleep(0)

    waiter_planner = world.planner("proc-b", provider)
    waiter_planner.sleep = watched_sleep
    waiter = asyncio.create_task(waiter_planner.ensure_fresh(CONN, flow=FLOW))
    await waiting.wait()  # B is polling A's lease
    provider.pause.set()

    assert await holder is True
    assert await waiter is False
    assert provider.calls == 1


async def test_invalid_client_is_terminal_consent_required(world: World) -> None:
    async def post(url: str, data: dict[str, str], auth: tuple[str, str]) -> tuple[int, Any]:
        assert data == {"grant_type": "refresh_token", "refresh_token": "r0"}
        assert auth == ("app-key", "app-secret")
        return 401, {"error": "invalid_client"}

    async def refresh(request: RefreshRequest) -> RenewedCredential:
        return await refresh_access_token(request, post=post)

    with pytest.raises(CredentialRenewalError) as caught:
        await world.planner("proc-a", refresh).ensure_fresh(CONN, flow=FLOW)
    assert caught.value.error_code == "consent_required" and caught.value.terminal
    record = await world.state.get(CONN)
    assert record is not None
    assert (record.status, record.reason_code) == ("needs_you", "consent_required")


async def test_retry_budget_is_bounded_at_twenty_seconds(world: World) -> None:
    assert world.planner("x", Provider()).provider_timeout == 20.0
    calls = 0

    async def flaky(_request: RefreshRequest) -> RenewedCredential:
        nonlocal calls
        calls += 1
        raise CredentialRenewalError(error_code="provider_unavailable", message="503")

    with pytest.raises(CredentialRenewalError) as caught:
        await world.planner("proc-a", flaky).ensure_fresh(CONN, flow=FLOW)
    assert caught.value.error_code == "provider_unavailable"
    assert calls == 3

    async def hangs(_request: RefreshRequest) -> RenewedCredential:
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    hanging = world.planner("proc-b", hangs, provider_timeout=0.05)
    with pytest.raises(CredentialRenewalError) as timed_out:
        await asyncio.wait_for(hanging.ensure_fresh(CONN, flow=FLOW), timeout=2)
    assert timed_out.value.error_code == "provider_unavailable"
    record = await world.state.get(CONN)
    assert record is not None and record.status != "needs_you"  # counted, not terminal


async def test_reconnect_during_a_renewal_is_never_overwritten(world: World) -> None:
    """An operator reconnect mid-renewal wins; the stale renewal commit is refused."""
    provider = Provider(rotate=True)
    provider.pause = asyncio.Event()
    renewing = asyncio.create_task(world.planner("proc-a", provider).ensure_fresh(CONN, flow=FLOW))
    await provider.entered.wait()
    await world.rows().put_fields(CONN, {"refresh_token": "operator-new"}, actor_did=ACTOR)
    provider.pause.set()

    with pytest.raises(CredentialRenewalError) as caught:
        await renewing
    assert caught.value.error_code == "renewal_commit_lost"
    assert await world.refresh_token() == "operator-new"
    row = await world.rows().read(CONN)
    assert row is not None and row.access is None and row.lease is None


async def test_refresh_maps_provider_answers() -> None:
    request = RefreshRequest(
        flow=FLOW, refresh_token=Secret("r"), client_id="id", client_secret=Secret("s")
    )

    def answering(status: int, body: dict[str, Any]) -> Any:
        async def post(*_args: Any) -> tuple[int, dict[str, Any]]:
            return status, body

        return post

    ok = await refresh_access_token(
        request,
        post=answering(200, {"access_token": "a", "expires_in": 14400, "refresh_token": "r2"}),
    )
    assert ok.access_token.reveal() == "a" and ok.expires_in == 14400
    assert ok.refresh_token is not None and ok.refresh_token.reveal() == "r2"
    for status, body, code, terminal in (
        (400, {"error": "invalid_grant"}, "invalid_grant", True),
        (403, {"error": "access_denied"}, "auth_required", True),
        (429, {"error": "slow_down", "_retry_after": 2}, "rate_limited", False),
        (503, {}, "provider_unavailable", False),
    ):
        with pytest.raises(CredentialRenewalError) as caught:
            await refresh_access_token(request, post=answering(status, body))
        assert (caught.value.error_code, caught.value.terminal) == (code, terminal)
