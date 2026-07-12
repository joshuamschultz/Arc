"""Atomic conditional write ``update_if`` on the mutable plane — RED.

Copies the single-owner claim pattern from ``arcgateway/pairing.py:788-794``
(``UPDATE ... WHERE <cond>`` -> ``rowcount>0``) onto the SPEC-056 Phase 0A
mutable plane. ``update_if`` does not exist yet — every test fails with
``AttributeError`` (feature absent), not an import/syntax error.

Per [[feedback_concurrency_tests_must_interleave]]: an instant mock lets
``asyncio.gather`` run both claimers sequentially and the race never fires.
``asyncio.Barrier`` forces both coroutines to reach the conditional write at
the same instant, so a non-atomic implementation (read-then-write) shows up
as a flake across repeated runs — hence the 100-run stress loop below.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arcstore.backends.sqlite import SqliteBackend

_SYSTEM = "did:arc:test:system"
_ACTOR_A = "did:arc:test:exec/aaaaaaaa"
_ACTOR_B = "did:arc:test:exec/bbbbbbbb"

_N_RUNS = 100


class TestUpdateIfConditionalCorrectness:
    async def test_matching_where_applies_patch_and_returns_true(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"owner": None}, actor_did=_SYSTEM)
            won = await be.update_if(
                "tasks", "t1", {"owner": _ACTOR_A}, where={"owner": None}, actor_did=_ACTOR_A
            )
            assert won is True
            got = await be.mutable_read("tasks", "t1")
            assert got is not None
            assert got["owner"] == _ACTOR_A
        finally:
            await be.stop()

    async def test_non_matching_where_returns_false_and_leaves_row_unchanged(
        self, tmp_path: Path
    ) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"owner": _ACTOR_A}, actor_did=_SYSTEM)
            won = await be.update_if(
                "tasks", "t1", {"owner": _ACTOR_B}, where={"owner": None}, actor_did=_ACTOR_B
            )
            assert won is False
            got = await be.mutable_read("tasks", "t1")
            assert got is not None
            assert got["owner"] == _ACTOR_A
        finally:
            await be.stop()

    async def test_missing_key_returns_false(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            won = await be.update_if(
                "tasks", "does-not-exist", {"owner": _ACTOR_A}, where={"owner": None},
                actor_did=_ACTOR_A,
            )
            assert won is False
        finally:
            await be.stop()

    async def test_patch_is_a_partial_merge_not_a_replace(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write(
                "tasks", "t1", {"title": "Fix bug", "owner": None}, actor_did=_SYSTEM
            )
            won = await be.update_if(
                "tasks", "t1", {"owner": _ACTOR_A}, where={"owner": None}, actor_did=_ACTOR_A
            )
            assert won is True
            got = await be.mutable_read("tasks", "t1")
            assert got is not None
            assert got["title"] == "Fix bug"
            assert got["owner"] == _ACTOR_A
        finally:
            await be.stop()


class TestUpdateIfSingleOwnerClaim:
    """G3/NFR-2 — two concurrent claimers on one key, exactly one wins."""

    async def test_two_concurrent_claimers_exactly_one_wins(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            await be.mutable_write("tasks", "t1", {"owner": None}, actor_did=_SYSTEM)
            barrier = asyncio.Barrier(2)  # forces real interleaving, not a sequential mock

            async def claim(owner: str) -> bool:
                await barrier.wait()
                return await be.update_if(
                    "tasks", "t1", {"owner": owner}, where={"owner": None}, actor_did=owner
                )

            won_a, won_b = await asyncio.gather(claim(_ACTOR_A), claim(_ACTOR_B))
            assert won_a != won_b, "exactly one of two concurrent claimers must win"
            got = await be.mutable_read("tasks", "t1")
            assert got is not None
            assert got["owner"] in (_ACTOR_A, _ACTOR_B)
        finally:
            await be.stop()


@pytest.mark.slow
class TestUpdateIfSingleOwnerClaimStress:
    """G1.3-style stress gate — 100 independent forced-interleave races.

    A non-atomic (read-then-write) implementation races only under real
    thread/scheduler contention, so a single run can pass by luck. 100
    consecutive single-winner outcomes makes a false-pass statistically
    negligible.
    """

    async def test_single_winner_holds_across_100_runs(self, tmp_path: Path) -> None:
        be = SqliteBackend(tmp_path / "store.db")
        await be.start()
        try:
            failures: list[int] = []
            for i in range(_N_RUNS):
                key = f"race-{i}"
                await be.mutable_write("tasks", key, {"owner": None}, actor_did=_SYSTEM)
                barrier = asyncio.Barrier(2)

                async def claim(owner: str, key: str = key, barrier: asyncio.Barrier = barrier) -> bool:
                    await barrier.wait()
                    return await be.update_if(
                        "tasks", key, {"owner": owner}, where={"owner": None}, actor_did=owner
                    )

                won_a, won_b = await asyncio.gather(claim(_ACTOR_A), claim(_ACTOR_B))
                if won_a == won_b:
                    failures.append(i)

            assert not failures, (
                f"race detected on {len(failures)}/{_N_RUNS} runs: {failures} — "
                "update_if is not atomic (both or neither claimer won)"
            )
        finally:
            await be.stop()
