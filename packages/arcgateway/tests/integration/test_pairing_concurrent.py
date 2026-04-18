"""Integration tests for PairingStore concurrent access patterns.

Tests:
- test_concurrent_mint_doesnt_violate_max_pending: asyncio.gather × 5 → at most 3 succeed
- test_concurrent_approval_race: two simultaneous verify_and_consume → exactly one succeeds
- test_concurrent_different_platforms: concurrent mints on different platforms are independent
- test_concurrent_cleanup_and_mint: cleanup + mint concurrently without corruption
- test_five_concurrent_failure_recordings: 5 concurrent failures lock the platform
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arcgateway.pairing import PairingPlatformFull, PairingRateLimited, PairingStore


# ---------------------------------------------------------------------------
# Concurrent mint tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_mint_doesnt_violate_max_pending(tmp_path: Path) -> None:
    """asyncio.gather × 5 mint_code calls → at most 3 succeed (max pending enforced).

    This is the core race-condition test. Even under concurrent pressure,
    the 3-pending-max invariant must hold.
    """
    store = PairingStore(db_path=tmp_path / "concurrent.db")

    # 5 different users requesting on the same platform simultaneously
    async def _try_mint(user_id: str) -> bool:
        try:
            await store.mint_code(platform="telegram", platform_user_id=user_id)
            return True
        except (PairingPlatformFull, PairingRateLimited):
            return False

    results = await asyncio.gather(
        _try_mint("user_A"),
        _try_mint("user_B"),
        _try_mint("user_C"),
        _try_mint("user_D"),
        _try_mint("user_E"),
    )

    successes = sum(results)
    assert successes <= 3, (
        f"Expected at most 3 concurrent mints to succeed, got {successes}. "
        "PairingPlatformFull invariant violated under concurrency."
    )
    assert successes >= 1, "At least 1 mint should succeed"


@pytest.mark.asyncio
async def test_concurrent_approval_race(tmp_path: Path) -> None:
    """Two simultaneous verify_and_consume calls → exactly one returns the code.

    Ensures the approval is atomic: the code cannot be double-consumed.
    """
    store = PairingStore(db_path=tmp_path / "race.db")
    code_obj = await store.mint_code(platform="telegram", platform_user_id="shared_user")

    results = await asyncio.gather(
        store.verify_and_consume(code_obj.code),
        store.verify_and_consume(code_obj.code),
        return_exceptions=True,
    )

    successes = [r for r in results if r is not None and not isinstance(r, Exception)]
    assert len(successes) == 1, (
        f"Expected exactly 1 successful consume, got {len(successes)}. "
        "Double-consume race detected."
    )


@pytest.mark.asyncio
async def test_concurrent_different_platforms_independent(tmp_path: Path) -> None:
    """Concurrent mints on different platforms are completely independent.

    Platform A filling up must not block Platform B.
    """
    store = PairingStore(db_path=tmp_path / "multi_platform.db")

    # Fill telegram to max (3 pending)
    await store.mint_code(platform="telegram", platform_user_id="t1")
    await store.mint_code(platform="telegram", platform_user_id="t2")
    await store.mint_code(platform="telegram", platform_user_id="t3")

    # Slack and Discord should still accept new codes concurrently
    async def _mint_other_platform(platform: str, user_id: str) -> bool:
        try:
            await store.mint_code(platform=platform, platform_user_id=user_id)
            return True
        except (PairingPlatformFull, PairingRateLimited):
            return False

    results = await asyncio.gather(
        _mint_other_platform("slack", "s1"),
        _mint_other_platform("discord", "d1"),
        _mint_other_platform("slack", "s2"),
    )
    assert all(results), (
        "Mints on non-full platforms must succeed even when telegram is at max"
    )


@pytest.mark.asyncio
async def test_concurrent_cleanup_and_mint(tmp_path: Path) -> None:
    """cleanup_expired and mint_code running concurrently must not corrupt state."""
    store = PairingStore(db_path=tmp_path / "cleanup_race.db")

    # Pre-seed with some codes on different platforms
    for i in range(3):
        await store.mint_code(platform=f"seed_plat_{i}", platform_user_id=f"seed_{i}")

    # Run cleanup and a new mint concurrently
    async def _try_mint() -> bool:
        try:
            await store.mint_code(platform="slack", platform_user_id="concurrent_user")
            return True
        except (PairingPlatformFull, PairingRateLimited):
            return False

    cleanup_task = store.cleanup_expired()
    mint_task = _try_mint()
    removed, minted = await asyncio.gather(cleanup_task, mint_task)

    # No assertion on removed count (0 expired codes were seeded) — just verify no crash
    assert isinstance(removed, int)
    assert isinstance(minted, bool)


@pytest.mark.asyncio
async def test_five_concurrent_failure_recordings(tmp_path: Path) -> None:
    """Five concurrent bad verify attempts must still trigger lockout.

    Even when failure recording is concurrent, the lockout threshold must
    be respected and the platform must end up locked.

    The gateway always passes platform context (platform_hint) so failures
    are attributed to the correct platform even for unknown codes.
    """
    store = PairingStore(db_path=tmp_path / "lockout_race.db")

    # Seed a code so there IS a live platform entry for telegram
    await store.mint_code(platform="telegram", platform_user_id="seed_user")

    # Five concurrent bad attempts, all attributed to telegram
    bad_attempts = [
        store.verify_and_consume(f"BADCODE{i}", platform_hint="telegram")
        for i in range(5)
    ]
    results = await asyncio.gather(*bad_attempts)
    assert all(r is None for r in results), "Bad codes must all return None"

    # Platform must be locked after 5 failures
    is_locked = await store.is_platform_locked("telegram")
    assert is_locked, "Platform must be locked after 5 concurrent failed attempts"
