"""Item 20 P20-1 — the WORM mirror verifies each row, incrementally, across rotation.

The Security screen read 0/100 VERIFIED because rows were stamped once with a
whole-chain verdict (or none, with no key) and frozen there. These tests drive
real signed chains through ``StoreIngest`` and assert the per-row verdict.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from arctrust import causal
from arctrust.audit import AuditEvent, WormSink, signer_fingerprint
from arctrust.keypair import KeyPair, generate_keypair
from arctrust.signer import InProcessSigner

from arcstore import query
from arcstore.backends.base import AUDIT_TABLE
from arcstore.backends.memory import FakeBackend
from arcstore.ingest import StoreIngest

_CHAIN = "audit-chain-olivia.jsonl"


def _event(i: int, *, outcome: str = "allow") -> AuditEvent:
    return AuditEvent(
        actor_did="did:arc:t:a/1", action="policy.evaluate", target=f"tool{i}", outcome=outcome
    )


class _World:
    def __init__(self, tmp_path: Path, *, max_records: int = 100_000) -> None:
        self.worm_dir = tmp_path / "data" / "worm"
        self.worm_dir.mkdir(parents=True)
        self.kp: KeyPair = generate_keypair()
        self.path = self.worm_dir / _CHAIN
        self.sink = WormSink(
            self.path, InProcessSigner(self.kp.private_key), max_records=max_records
        )
        self.backend = FakeBackend()

    def ingest(self, *, key: bool = True) -> StoreIngest:
        return StoreIngest(
            self.backend,
            spool_dir=self.worm_dir.parent / "spool",
            worm_dir=self.worm_dir,
            worm_public_key=self.kp.public_key if key else None,
        )

    def write(self, n: int, start: int = 0, **kwargs: Any) -> None:
        for i in range(start, start + n):
            self.sink.write(_event(i, **kwargs))

    async def rows(self) -> list[dict[str, Any]]:
        rows = await self.backend.query(AUDIT_TABLE)
        return sorted(
            (r for r in rows if r.get("action") != "audit.chain.broken"), key=lambda r: r["seq"]
        )

    async def broken(self) -> list[dict[str, Any]]:
        return await self.backend.query(AUDIT_TABLE, where={"action": "audit.chain.broken"})

    def tamper(self, seq: int) -> None:
        self.sink.close()
        lines = self.path.read_text().splitlines()
        record = json.loads(lines[seq])
        record["event"]["outcome"] = "deny"
        lines[seq] = json.dumps(record)
        self.path.write_text("\n".join(lines) + "\n")


async def test_ingest_with_the_key_verifies_every_row(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.write(4)
    await world.ingest().scan_once()
    rows = await world.rows()
    assert [r["verified"] for r in rows] == [True] * 4
    totals = await query.audit_totals(world.backend)
    assert totals == {"total": 4, "verified": 4, "broken": 0}


async def test_ingest_without_a_key_verifies_nothing(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.write(2)
    await world.ingest(key=False).scan_once()
    assert [r["verified"] for r in await world.rows()] == [False, False]


async def test_tailing_verifies_only_new_records_against_the_stored_tip(tmp_path: Path) -> None:
    world = _World(tmp_path)
    ingest = world.ingest()
    world.write(3)
    await ingest.scan_once()
    world.write(2, start=3)
    await ingest.scan_once()
    assert [r["verified"] for r in await world.rows()] == [True] * 5


async def test_a_restarted_ingest_resumes_the_chain_tip(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.write(3)
    await world.ingest().scan_once()
    world.write(2, start=3)
    await world.ingest().scan_once()  # a fresh instance over the same DB + files
    rows = await world.rows()
    assert len(rows) == 5
    assert all(r["verified"] for r in rows)


async def test_a_tampered_record_flips_it_and_everything_after(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.write(5)
    await world.ingest().scan_once()
    world.tamper(2)

    ingest = world.ingest()
    summary = await ingest.reverify()

    verdicts = {r["seq"]: r["verified"] for r in await world.rows()}
    assert verdicts == {0: True, 1: True, 2: False, 3: False, 4: False}
    broken = await world.broken()
    assert [b["seq"] for b in broken] == [2]
    assert summary["broken"] == 1
    totals = await query.audit_totals(world.backend)
    assert totals["broken"] == 1


async def test_a_forged_record_appended_after_ingest_is_flagged(tmp_path: Path) -> None:
    world = _World(tmp_path)
    world.write(2)
    ingest = world.ingest()
    await ingest.scan_once()
    world.sink.close()
    replay = json.loads(world.path.read_text().splitlines()[1])
    with world.path.open("a") as fh:  # a replayed record: valid signature, stale seq
        fh.write(json.dumps(replay | {"event_hash": replay["event_hash"][::-1]}) + "\n")
    await ingest.scan_once()
    assert [b["seq"] for b in await world.broken()] == [2]


async def test_reverify_repairs_rows_frozen_unverified(tmp_path: Path) -> None:
    """The DGX state: every row was mirrored with no key and stays false forever."""
    world = _World(tmp_path)
    world.write(3)
    await world.ingest(key=False).scan_once()
    assert not any(r["verified"] for r in await world.rows())

    summary = await world.ingest().reverify()

    assert all(r["verified"] for r in await world.rows())
    assert summary == {"events": 3, "verified": 3, "broken": 0}


async def test_rotated_segments_verify_and_never_duplicate(tmp_path: Path) -> None:
    world = _World(tmp_path, max_records=2)
    ingest = world.ingest()
    world.write(1)
    await ingest.scan_once()
    world.write(4, start=1)  # rotates twice; seq 0 is now inside a renamed segment
    await ingest.scan_once()
    world.write(1, start=5)
    await ingest.scan_once()
    rows = await world.rows()
    assert [r["seq"] for r in rows] == [0, 1, 2, 3, 4, 5]
    assert all(r["verified"] for r in rows)
    assert await world.broken() == []


async def test_rows_project_the_signer_and_causal_chain(tmp_path: Path) -> None:
    world = _World(tmp_path)
    agent = "did:arc:t:a/1"
    with causal.bind(causal.root("agent", agent)), causal.refine(run_id="r1", tool_call_id="c1"):
        world.write(1, outcome="deny")
    await world.ingest().scan_once()
    (row,) = await world.rows()
    assert row["signer"] == signer_fingerprint(world.kp.public_key)
    assert row["chain"] == "audit-chain-olivia"
    assert row["initiator"] == "agent"
    assert row["run_id"] == "r1"
    assert row["tool_call_id"] == "c1"
    assert row["causal"]["initiator_id"] == agent
    assert row["is_denial"] is True
    assert row["is_control"] is False
