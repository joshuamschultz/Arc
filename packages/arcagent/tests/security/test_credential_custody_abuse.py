"""P18-2 §9 — abuse cases for sealed connector credential custody.

Replayed refresh commits, a TOCTOU swap of ciphertext between connections, stale
handles after revoke, cross-agent reads, leakage into logs/audit/results, a database
reader without the operator key, a forged lease hog, a symlinked legacy file, and a
downgrade restore of an old row.
"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from arctrust.audit import AuditEvent
from arctrust.secrets import SECRET_PATTERNS
from packages.arcagent.tests.custody_fakes import InterleavingBackend, make_cipher

from arcagent.core.errors import ExtensionError
from arcagent.extension.connection_health import StoreHealthReporter
from arcagent.extension.credential_broker import AccessTokenBroker, CredentialPlan
from arcagent.extension.credentials import (
    CredentialRenewalError,
    RefreshRequest,
    RenewalPlanner,
    RenewedCredential,
)
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcagent.extension.custody_migrate import migrate_connector_secrets
from arcagent.extension.grants import Connection, ConnectionRegistry
from arcagent.extension.manifest import OAuthFlow
from arcagent.extension.secrets import Secret
from arcagent.extension.state import ConnectionRecord, ConnectionStateStore

FLOW = OAuthFlow(
    authorize_url="https://auth.example/authorize",
    token_url="https://auth.example/token",
    client_id_secret="app_key",
    client_secret_secret="app_secret",
    refresh_token_secret="refresh_token",
)
PLAN = CredentialPlan(
    oauth=FLOW,
    bearer_field=None,
    handle_fields=frozenset(),
    withheld=frozenset({"refresh_token", "app_secret"}),
)
ACTOR = "did:arc:operator:test"
REFRESH, CLIENT_SECRET = "refresh-secret-abc", "client-secret-xyz"


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class Provider:
    """Rotating, single-use refresh tokens; records every token it was shown."""

    def __init__(self) -> None:
        self.current = REFRESH
        self.consumed: set[str] = set()
        self.presented: list[str] = []
        self.issued = 0

    async def __call__(self, request: RefreshRequest) -> RenewedCredential:
        token = request.refresh_token.reveal()
        self.presented.append(token)
        if token in self.consumed or token != self.current:
            raise CredentialRenewalError(error_code="invalid_grant", message="invalid_grant")
        self.consumed.add(token)
        self.issued += 1
        self.current = f"rotated-{self.issued}"
        return RenewedCredential(
            access_token=Secret(f"access-{self.issued}"),
            expires_in=3600,
            refresh_token=Secret(self.current),
        )


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def _fast_sleep(_seconds: float) -> None:
    return None


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.backend = InterleavingBackend()
        self.clock = Clock()
        self.sink = ListSink()
        self.state = ConnectionStateStore(self.backend)
        self.provider = Provider()
        self.arc_dir = tmp_path / "arc"
        self.registry = ConnectionRegistry(self.arc_dir)

    async def open(self) -> Any:
        return self.backend

    def rows(self, label: str = "test") -> CredentialRowStore:
        return CredentialRowStore(
            self.backend, make_cipher(label), sink=self.sink, clock=self.clock
        )

    def planner(self, owner: str) -> RenewalPlanner:
        return RenewalPlanner(
            rows=self.rows(),
            refresh=self.provider,
            health=StoreHealthReporter(self.open),
            owner_id=owner,
            state=self.state,
            sink=self.sink,
            clock=self.clock,
            sleep=_fast_sleep,
        )

    def broker(self, agent: str, did: str) -> AccessTokenBroker:
        return AccessTokenBroker(
            self.rows(),
            registry=lambda: ConnectionRegistry(self.arc_dir),
            renewals=self.planner(f"agent-{agent}"),
            health=StoreHealthReporter(self.open),
            sink=self.sink,
            clock=self.clock,
            bound_agent=agent,
            bound_did=did,
        )

    async def seed(self, connection: str, refresh: str = REFRESH) -> None:
        await self.rows().put_fields(
            connection,
            {"app_key": "app-key", "app_secret": CLIENT_SECRET, "refresh_token": refresh},
            actor_did=ACTOR,
        )
        await self.state.create(
            ConnectionRecord(connection=connection, custody="arc"), actor_did=ACTOR
        )


@pytest.fixture
async def world(tmp_path: Path) -> World:
    built = World(tmp_path)
    await built.seed("blackarc")
    built.registry.define(
        "blackarc", Connection(extension="box", approval="auto", agents=("josh",))
    )
    return built


async def test_replayed_refresh_commit_is_refused(world: World) -> None:
    rows = world.rows()
    ttl = timedelta(seconds=60)
    lease_a = await rows.acquire_lease("blackarc", "proc-a", ttl)
    assert lease_a is not None
    world.clock.now += timedelta(seconds=61)
    lease_b = await rows.acquire_lease("blackarc", "proc-b", ttl)
    assert lease_b is not None
    kwargs: dict[str, Any] = {
        "issued_at": world.clock.now,
        "expires_at": world.clock.now + timedelta(hours=1),
        "scope": None,
        "refresh_field": "refresh_token",
    }
    assert await rows.commit_renewal(lease_b, access_token="b", rotated_refresh="rb", **kwargs)
    before = await world.backend.mutable_read(CREDENTIAL_COLLECTION, "blackarc")
    assert not await rows.commit_renewal(lease_a, access_token="a", rotated_refresh="ra", **kwargs)
    assert not await rows.commit_renewal(lease_b, access_token="a", rotated_refresh="ra", **kwargs)
    after = await world.backend.mutable_read(CREDENTIAL_COLLECTION, "blackarc")
    assert {k: v for k, v in (after or {}).items() if k != "updated_at"} == {
        k: v for k, v in (before or {}).items() if k != "updated_at"
    }


async def test_rotate_toctou_swap_of_ciphertext_fails_closed(world: World) -> None:
    await world.seed("systems", refresh="systems-refresh")
    other = await world.backend.mutable_read(CREDENTIAL_COLLECTION, "systems")
    assert other is not None
    mine = await world.backend.mutable_read(CREDENTIAL_COLLECTION, "blackarc")
    assert mine is not None
    fields = dict(mine["fields"])
    fields["refresh_token"] = other["fields"]["refresh_token"]  # attacker with DB write
    await world.backend.mutable_merge(
        CREDENTIAL_COLLECTION, "blackarc", {"fields": fields}, actor_did="did:attacker"
    )

    with pytest.raises(CredentialRenewalError) as caught:
        await world.planner("proc-a").ensure_fresh("blackarc", flow=FLOW)
    assert caught.value.error_code == "credential_unreadable"
    record = await world.state.get("blackarc")
    assert record is not None and record.status == "needs_you"
    assert world.provider.presented == []


async def test_stale_handle_after_revoke_is_dead(world: World) -> None:
    handle = world.broker("josh", "did:arc:agent:josh").handle(
        "blackarc", agent="josh", agent_did="did:arc:agent:josh", plan=PLAN
    )
    assert (await handle.bearer()).reveal() == "access-1"
    world.registry.define("blackarc", Connection(extension="box", approval="auto", agents=()))
    world.registry.forget("blackarc")
    world.clock.now += timedelta(hours=2)  # the token is stale, a renewal would be due
    with pytest.raises(ExtensionError) as caught:
        await handle.bearer()
    assert caught.value.code == "CREDENTIAL_NOT_GRANTED"
    assert world.provider.presented == [REFRESH]
    assert any(e.action == "secret.read" and e.outcome == "deny" for e in world.sink.events)


async def test_cross_agent_token_read_is_denied(world: World) -> None:
    sales = world.broker("sales", "did:arc:agent:sales")
    handle = sales.handle("blackarc", agent="sales", agent_did="did:arc:agent:sales", plan=PLAN)
    with pytest.raises(ExtensionError) as caught:
        await handle.bearer()
    assert caught.value.code == "CREDENTIAL_NOT_GRANTED"
    with pytest.raises(ExtensionError):
        await sales.bearer("blackarc", agent="josh", agent_did="did:arc:agent:sales", plan=PLAN)
    with pytest.raises(ExtensionError):
        sales.handle("blackarc", agent="josh", agent_did="did:arc:agent:josh", plan=PLAN)
    assert world.provider.presented == []


async def test_refresh_token_never_reaches_a_tool_result_log_or_audit(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    handle = world.broker("josh", "did:arc:agent:josh").handle(
        "blackarc", agent="josh", agent_did="did:arc:agent:josh", plan=PLAN
    )
    results = []
    for index in range(20):
        if index == 10:
            await handle.invalidate()
        results.append(repr(await handle.bearer()))
    corpus = "\n".join([caplog.text, repr([e.model_dump() for e in world.sink.events]), *results])
    for secret in (REFRESH, CLIENT_SECRET, "rotated-1", "access-1", "access-2"):
        assert secret not in corpus
    assert not any(pattern.search(corpus) for _name, pattern in SECRET_PATTERNS)


async def test_db_reader_without_operator_key_learns_nothing(world: World) -> None:
    await world.planner("proc-a").ensure_fresh("blackarc", flow=FLOW)
    dump = repr(await world.backend.mutable_query(CREDENTIAL_COLLECTION))
    for secret in (REFRESH, CLIENT_SECRET, "rotated-1", "access-1"):
        assert secret not in dump
    stranger = world.rows("another-operator")
    row = await stranger.read("blackarc")
    assert row is not None
    with pytest.raises(ExtensionError) as caught:
        stranger.open_field(row, "refresh_token")
    assert caught.value.code == "CREDENTIAL_UNREADABLE"


async def test_lease_hog_cannot_block_renewal_forever(world: World) -> None:
    far = (world.clock.now + timedelta(days=365)).isoformat()
    await world.backend.mutable_merge(
        CREDENTIAL_COLLECTION,
        "blackarc",
        {"lease": {"owner": "attacker", "fence": 999, "expires_at": far}},
        actor_did="did:attacker",
    )
    assert await world.planner("proc-a").ensure_fresh("blackarc", flow=FLOW) is True
    assert any(e.action == "connection.credential.lease_contended" for e in world.sink.events)


async def test_symlinked_connections_env_is_refused_by_migrator(
    world: World, tmp_path: Path
) -> None:
    target = tmp_path / "passwd-like"
    target.write_text("root:x:0:0\nARC_SECRET_BLACKARC_APP_SECRET=leak\n")
    env = tmp_path / "connections.env"
    env.symlink_to(target)
    with pytest.raises(ExtensionError):
        await migrate_connector_secrets(
            env_path=env,
            registry=world.registry,
            declared_fields=lambda _i, _e: ("app_secret",),
            secret_store=None,
            verify_store=None,
            actor_did=ACTOR,
            sink=world.sink,
        )
    assert env.is_symlink() and os.path.exists(target)
    row = await world.rows().read("blackarc")
    assert row is not None
    assert world.rows().open_field(row, "app_secret").reveal() == CLIENT_SECRET  # type: ignore[union-attr]


async def test_downgrade_restore_of_old_row_is_honest(world: World) -> None:
    snapshot = await world.backend.mutable_read(CREDENTIAL_COLLECTION, "blackarc")
    assert snapshot is not None
    assert await world.planner("proc-a").ensure_fresh("blackarc", flow=FLOW) is True
    restored = {k: v for k, v in snapshot.items() if k != "updated_at"}
    await world.backend.mutable_write(
        CREDENTIAL_COLLECTION, "blackarc", restored, actor_did="did:attacker"
    )
    with pytest.raises(CredentialRenewalError) as caught:
        await world.planner("proc-b").ensure_fresh("blackarc", flow=FLOW)
    assert caught.value.error_code == "invalid_grant"
    record = await world.state.get("blackarc")
    assert record is not None and record.status == "needs_you"
    presented = len(world.provider.presented)
    for owner in ("proc-c", "proc-d"):
        with pytest.raises(CredentialRenewalError):
            await world.planner(owner).ensure_fresh("blackarc", flow=FLOW, force=True)
    assert len(world.provider.presented) == presented
