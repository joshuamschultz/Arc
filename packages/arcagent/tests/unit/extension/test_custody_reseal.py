"""P18-2F — re-sealing in-process custody rows under the Transit cipher.

An enterprise deployment that ran ``custody = "in_process"`` has rows sealed
under the operator-seed key (``xc1``). Moving to ``vault_transit`` re-seals each
row in ONE compare-and-set, verified before it is written. The migration is
idempotent (a second run does nothing) and crash-safe (a crash leaves every row
wholly under one cipher, and a re-run finishes the job).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust import FileNotaryTransit, TransitConnectorCipher
from arctrust.audit import AuditEvent
from packages.arcagent.tests.custody_fakes import InterleavingBackend, make_cipher

from arcagent.core.errors import ExtensionError
from arcagent.extension.custody import CREDENTIAL_COLLECTION, CredentialRowStore
from arcagent.extension.custody_migrate import reseal_connector_secrets

ACTOR = "did:arc:operator:test"


class ListSink:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def write(self, event: AuditEvent) -> None:
        self.events.append(event)


class World:
    def __init__(self, tmp_path: Path) -> None:
        self.backend = InterleavingBackend()
        self.sink = ListSink()
        self.old = CredentialRowStore(self.backend, make_cipher())
        self.target = TransitConnectorCipher(FileNotaryTransit(tmp_path / "notary"))
        self.new = CredentialRowStore(self.backend, self.target, sink=self.sink)

    async def seed(self) -> None:
        await self.old.put_fields(
            "blackarc", {"app_key": "k", "refresh_token": "r-1"}, actor_did=ACTOR
        )
        await self.old.put_fields("work_slack", {"user_token": "xoxp-1"}, actor_did=ACTOR)

    async def ciphers(self) -> dict[str, str]:
        rows = await self.backend.mutable_query(CREDENTIAL_COLLECTION)
        return {row["connection"]: row["cipher"] for row in rows}

    async def value(self, connection: str, field: str) -> str:
        row = await self.new.read(connection)
        assert row is not None
        found = await self.new.open_field(row, field)
        assert found is not None
        return found.reveal()

    async def run(self) -> Any:
        return await reseal_connector_secrets(
            self.new, source=make_cipher(), actor_did=ACTOR, sink=self.sink
        )


@pytest.fixture
async def world(tmp_path: Path) -> World:
    built = World(tmp_path)
    await built.seed()
    return built


async def test_reseal_moves_every_row_and_keeps_values(world: World) -> None:
    before = {
        row["connection"]: row for row in await world.backend.mutable_query(CREDENTIAL_COLLECTION)
    }

    report = await world.run()

    assert report.resealed == ("blackarc", "work_slack")
    assert await world.ciphers() == {"blackarc": "transit1", "work_slack": "transit1"}
    assert await world.value("blackarc", "refresh_token") == "r-1"
    assert await world.value("work_slack", "user_token") == "xoxp-1"
    after = {
        row["connection"]: row for row in await world.backend.mutable_query(CREDENTIAL_COLLECTION)
    }
    assert after["blackarc"]["generation"] == before["blackarc"]["generation"]
    raw = repr(after)
    assert "r-1" not in raw and "xoxp-1" not in raw
    resealed = [e for e in world.sink.events if e.action == "connection.credential.resealed"]
    assert {e.target for e in resealed} == {"connection:blackarc", "connection:work_slack"}
    summary = [e for e in world.sink.events if e.action == "connection.credential.resealed_all"]
    assert summary and summary[0].extra["count"] == 2
    assert "r-1" not in repr([e.model_dump() for e in world.sink.events])


async def test_reseal_is_idempotent(world: World) -> None:
    await world.run()
    calls = len(world.backend.update_if_calls)

    again = await world.run()

    assert again.resealed == ()
    assert again.already == ("blackarc", "work_slack")
    assert len(world.backend.update_if_calls) == calls


async def test_crash_mid_reseal_leaves_whole_rows_and_a_rerun_finishes(world: World) -> None:
    async def crash(collection: str, key: str) -> None:
        if key == "work_slack":
            raise RuntimeError("process killed")

    world.backend.before_update_if = crash
    with pytest.raises(RuntimeError):
        await world.run()

    assert await world.ciphers() == {"blackarc": "transit1", "work_slack": "xc1"}
    row = await world.old.read("work_slack")
    assert row is not None
    found = await world.old.open_field(row, "user_token")
    assert found is not None and found.reveal() == "xoxp-1"

    world.backend.before_update_if = None
    report = await world.run()
    assert report.resealed == ("work_slack",)
    assert await world.ciphers() == {"blackarc": "transit1", "work_slack": "transit1"}
    assert await world.value("work_slack", "user_token") == "xoxp-1"


async def test_concurrent_change_during_reseal_is_not_lost(world: World) -> None:
    async def operator_writes_first(collection: str, key: str) -> None:
        if key == "work_slack" and not changed:
            changed.append(True)
            world.backend.before_update_if = None
            await world.old.put_fields("work_slack", {"user_token": "xoxp-2"}, actor_did=ACTOR)

    changed: list[bool] = []
    world.backend.before_update_if = operator_writes_first

    await world.run()

    assert await world.value("work_slack", "user_token") == "xoxp-2"


async def test_transit_down_reseals_nothing_and_says_so(world: World, tmp_path: Path) -> None:
    (tmp_path / "notary").write_text("not a keystore")

    with pytest.raises(ExtensionError) as caught:
        await world.run()

    assert caught.value.code == "CREDENTIAL_CUSTODY_UNAVAILABLE"
    assert await world.ciphers() == {"blackarc": "xc1", "work_slack": "xc1"}


async def test_row_under_a_foreign_key_is_refused_not_rewritten(world: World) -> None:
    with pytest.raises(ExtensionError) as caught:
        await reseal_connector_secrets(
            world.new, source=make_cipher("another-seed"), actor_did=ACTOR, sink=world.sink
        )
    assert caught.value.code == "CREDENTIAL_UNREADABLE"
    assert set((await world.ciphers()).values()) == {"xc1"}
