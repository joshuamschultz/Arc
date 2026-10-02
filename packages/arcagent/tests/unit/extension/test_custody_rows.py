"""P18-2 §8.2 — the sealed custody row and its compare-and-set primitives."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from packages.arcagent.tests.custody_fakes import InterleavingBackend, make_cipher, once_per_task

from arcagent.core.errors import ExtensionError
from arcagent.extension.custody import (
    CREDENTIAL_COLLECTION,
    CredentialRowStore,
    SealedCredentialBackend,
)
from arcagent.extension.secrets import SecretRef, SecretStore

ACTOR = "did:arc:operator:test"


@pytest.fixture
def backend() -> InterleavingBackend:
    return InterleavingBackend()


@pytest.fixture
def rows(backend: InterleavingBackend) -> CredentialRowStore:
    return CredentialRowStore(backend, make_cipher())


async def test_put_fields_is_cas_and_loses_no_field(
    backend: InterleavingBackend, rows: CredentialRowStore
) -> None:
    await rows.put_fields("blackarc", {"app_key": "k0"}, actor_did=ACTOR)
    before = await rows.read("blackarc")
    assert before is not None
    barrier = asyncio.Barrier(2)
    backend.before_update_if = once_per_task(barrier.wait, collection=CREDENTIAL_COLLECTION)

    await asyncio.gather(
        rows.put_fields("blackarc", {"app_key": "k1"}, actor_did=ACTOR),
        rows.put_fields("blackarc", {"app_secret": "s1"}, actor_did=ACTOR),
    )

    after = await rows.read("blackarc")
    assert after is not None
    assert {name for name in after.fields} == {"app_key", "app_secret"}
    assert rows.open_field(after, "app_key").reveal() == "k1"  # type: ignore[union-attr]
    assert rows.open_field(after, "app_secret").reveal() == "s1"  # type: ignore[union-attr]
    assert after.generation == before.generation + 2
    # Both writers really did race: three CAS attempts for two writes.
    attempts = [call for call in backend.update_if_calls if call[0] == CREDENTIAL_COLLECTION]
    assert len(attempts) >= 3


async def test_every_cas_key_is_present_on_create(
    backend: InterleavingBackend, rows: CredentialRowStore
) -> None:
    await rows.put_fields("blackarc", {"app_key": "k"}, actor_did=ACTOR)
    raw = await backend.mutable_read(CREDENTIAL_COLLECTION, "blackarc")
    assert raw is not None
    for key in ("revision", "generation", "fields", "access", "lease", "fence_counter", "cipher"):
        assert key in raw
    assert raw["access"] is None
    assert raw["lease"] is None
    assert raw["cipher"] == "xc1"
    assert "k" not in str(raw["fields"]["app_key"]["sealed"])[:6]


async def test_cipher_kind_mismatch_is_unreadable(
    backend: InterleavingBackend, rows: CredentialRowStore
) -> None:
    await rows.put_fields("blackarc", {"refresh_token": "r"}, actor_did=ACTOR)
    await backend.mutable_merge(
        CREDENTIAL_COLLECTION, "blackarc", {"cipher": "transit1"}, actor_did=ACTOR
    )
    row = await rows.read("blackarc")
    assert row is not None
    with pytest.raises(ExtensionError) as caught:
        rows.open_field(row, "refresh_token")
    assert caught.value.code == "CREDENTIAL_UNREADABLE"


async def test_value_sealed_under_another_key_is_unreadable(
    backend: InterleavingBackend, rows: CredentialRowStore
) -> None:
    await rows.put_fields("blackarc", {"refresh_token": "r"}, actor_did=ACTOR)
    other = CredentialRowStore(backend, make_cipher("other-operator"))
    row = await other.read("blackarc")
    assert row is not None
    with pytest.raises(ExtensionError) as caught:
        other.open_field(row, "refresh_token")
    assert caught.value.code == "CREDENTIAL_UNREADABLE"


async def test_forget_removes_undeclared_leftovers(rows: CredentialRowStore) -> None:
    await rows.put_fields(
        "blackarc", {"app_key": "k", "legacy_orphan": "x", "refresh_token": "r"}, actor_did=ACTOR
    )
    assert await rows.forget("blackarc", actor_did=ACTOR) is True
    assert await rows.read("blackarc") is None
    assert await rows.forget("blackarc", actor_did=ACTOR) is False


async def test_delete_fields_bumps_generation_and_reports_removed(
    rows: CredentialRowStore,
) -> None:
    generation = await rows.put_fields("c", {"a": "1", "b": "2"}, actor_did=ACTOR)
    removed = await rows.delete_fields("c", ["a", "missing"], actor_did=ACTOR)
    assert removed == ("a",)
    row = await rows.read("c")
    assert row is not None and set(row.fields) == {"b"}
    assert row.generation == generation + 1
    assert await rows.delete_fields("absent", ["a"], actor_did=ACTOR) == ()


async def test_sealed_backend_serves_secret_store(backend: InterleavingBackend) -> None:
    store = SecretStore(SealedCredentialBackend(CredentialRowStore(backend, make_cipher())))
    ref = SecretRef(connection="blackarc", field="user_token")
    assert await store.get(ref, caller_did=ACTOR) is None
    await store.put(ref, "xoxp-1", caller_did=ACTOR)
    found = await store.get(ref, caller_did=ACTOR)
    assert found is not None and found.reveal() == "xoxp-1"
    assert await store.delete(ref, caller_did=ACTOR) is True
    assert await store.get(ref, caller_did=ACTOR) is None
    raw = await backend.mutable_read(CREDENTIAL_COLLECTION, "blackarc")
    assert raw is not None and "xoxp-1" not in str(raw)


async def test_put_grant_stores_refresh_and_access_in_one_write(
    backend: InterleavingBackend, rows: CredentialRowStore
) -> None:
    await rows.put_fields("box", {"app_key": "k"}, actor_did=ACTOR)
    before = await rows.read("box")
    assert before is not None
    issued = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)
    writes = len(backend.update_if_calls)

    generation = await rows.put_grant(
        "box",
        refresh_field="refresh_token",
        refresh_token="r-1",
        access_token="a-1",
        issued_at=issued,
        expires_at=issued + timedelta(hours=4),
        scope=None,
        actor_did=ACTOR,
    )

    assert len(backend.update_if_calls) == writes + 1
    row = await rows.read("box")
    assert row is not None and row.generation == generation == before.generation + 1
    assert rows.open_field(row, "refresh_token").reveal() == "r-1"  # type: ignore[union-attr]
    assert rows.open_field(row, "app_key").reveal() == "k"  # type: ignore[union-attr]
    access = rows.open_access(row)
    assert access is not None and access.token.reveal() == "a-1"
    assert access.expires_at == issued + timedelta(hours=4)


async def test_lease_is_exclusive_and_fenced(rows: CredentialRowStore) -> None:
    await rows.put_fields("c", {"refresh_token": "r"}, actor_did=ACTOR)
    ttl = timedelta(seconds=60)
    first = await rows.acquire_lease("c", "proc-a", ttl)
    assert first is not None
    assert await rows.acquire_lease("c", "proc-b", ttl) is None
    await rows.release_lease(first)
    second = await rows.acquire_lease("c", "proc-b", ttl)
    assert second is not None and second.fence == first.fence + 1


async def test_expired_lease_is_taken_over(backend: InterleavingBackend) -> None:
    now = [datetime(2026, 10, 2, 12, 0, tzinfo=UTC)]
    rows = CredentialRowStore(backend, make_cipher(), clock=lambda: now[0])
    await rows.put_fields("c", {"refresh_token": "r"}, actor_did=ACTOR)
    stale = await rows.acquire_lease("c", "proc-a", timedelta(seconds=60))
    assert stale is not None
    now[0] += timedelta(seconds=61)
    taken = await rows.acquire_lease("c", "proc-b", timedelta(seconds=60))
    assert taken is not None and taken.fence > stale.fence
    assert await rows.renew_lease(stale, timedelta(seconds=60)) is False
