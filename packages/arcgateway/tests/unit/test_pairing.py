"""Unit tests for PairingStore — DM pairing code generation and lifecycle.

TDD tests written BEFORE implementation per Arc CLAUDE.md process.

Covers (T1.8.5 security test + full T1.8.1 acceptance):
- test_code_generation_alphabet: all chars from PAIRING_ALPHABET
- test_code_generation_collision_rate: all codes unique across 1000 mints
- test_ttl_enforced: verify_and_consume returns None after expiry
- test_max_3_pending_per_platform: 4th mint raises PairingPlatformFull
- test_rate_limit_per_user_10min: 2nd mint within 10min raises PairingRateLimited
- test_5_failed_approvals_lockout: 5 failures → platform locked for 1h
- test_no_raw_user_id_in_storage: only hashed user IDs in DB
- test_cleanup_expired_sweep: cleanup_expired removes only expired rows
- test_federal_requires_approver_did: federal tier rejects no-DID approval
- test_list_pending: returns only unexpired, unconsumed codes
- test_revoke: revoke removes code; verify returns None
- test_is_platform_locked: fresh platform not locked
- test_lockout_expires_after_1h: lockout expires after TTL
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from pathlib import Path

import pytest

from arcgateway.pairing import (
    PAIRING_ALPHABET,
    PairingCode,
    PairingPlatformFull,
    PairingRateLimited,
    PairingStore,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def store(tmp_path: Path) -> PairingStore:
    """Fresh PairingStore backed by a temp-dir SQLite DB."""
    return PairingStore(db_path=tmp_path / "pairing.db")


@pytest.fixture
def federal_store(tmp_path: Path) -> PairingStore:
    """PairingStore in federal tier mode (requires approver_did)."""
    return PairingStore(db_path=tmp_path / "pairing_federal.db", federal_tier=True)


# ---------------------------------------------------------------------------
# Code generation tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_code_generation_alphabet(tmp_path: Path) -> None:
    """All characters in generated codes must come from PAIRING_ALPHABET.

    Uses dedicated store per test; consumes each code immediately after
    minting to avoid hitting the 3-pending-per-platform cap.
    """
    store = PairingStore(db_path=tmp_path / "alphabet.db")
    allowed = set(PAIRING_ALPHABET)
    for i in range(1000):
        # Use a unique platform+user pair so rate-limit and pending cap never
        # trigger. platform_i gives us unlimited platforms.
        code = await store.mint_code(
            platform=f"platform_{i // 3}",  # 3 users per platform, rotates
            platform_user_id=f"user_{i}",
        )
        assert len(code.code) == 8, f"Code length must be 8, got {len(code.code)}"
        bad_chars = set(code.code) - allowed
        assert not bad_chars, f"Code {code.code!r} contains invalid chars: {bad_chars}"
        # Consume immediately to keep pending count at 0
        await store.verify_and_consume(code.code)


@pytest.mark.asyncio
async def test_code_generation_collision_rate(tmp_path: Path) -> None:
    """1000 codes should all be unique (32^8 ≈ 1.1 trillion combos).

    Consume each code immediately so the 3-pending-per-platform cap
    never interferes with this test's intent.
    """
    store = PairingStore(db_path=tmp_path / "collision.db")
    codes: set[str] = set()
    for i in range(1000):
        code = await store.mint_code(
            platform=f"plat_{i // 3}",
            platform_user_id=f"user_{i}",
        )
        assert code.code not in codes, f"Collision detected: {code.code}"
        codes.add(code.code)
        await store.verify_and_consume(code.code)
    assert len(codes) == 1000


@pytest.mark.asyncio
async def test_code_is_8_chars(store: PairingStore) -> None:
    """Code must be exactly 8 characters."""
    code = await store.mint_code(platform="slack", platform_user_id="user_x")
    assert len(code.code) == 8


# ---------------------------------------------------------------------------
# TTL / expiry tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ttl_enforced(tmp_path: Path) -> None:
    """verify_and_consume must return None for an expired code."""
    store = PairingStore(db_path=tmp_path / "p.db")
    code = await store.mint_code(platform="telegram", platform_user_id="alice")

    # Manually expire the code by backdating expires_at in the DB
    conn = sqlite3.connect(str(tmp_path / "p.db"))
    conn.execute(
        "UPDATE pairing_codes SET expires_at = ? WHERE code = ?",
        (time.time() - 1.0, code.code),
    )
    conn.commit()
    conn.close()

    result = await store.verify_and_consume(code.code)
    assert result is None, "Expired code must return None"


@pytest.mark.asyncio
async def test_valid_code_returns_pairing_code(store: PairingStore) -> None:
    """verify_and_consume returns PairingCode for a valid, unexpired code."""
    code = await store.mint_code(platform="telegram", platform_user_id="bob")
    result = await store.verify_and_consume(code.code)
    assert result is not None
    assert result.code == code.code


@pytest.mark.asyncio
async def test_consumed_code_not_reusable(store: PairingStore) -> None:
    """A code consumed once must return None on second attempt."""
    code = await store.mint_code(platform="telegram", platform_user_id="carol")
    first = await store.verify_and_consume(code.code)
    assert first is not None
    second = await store.verify_and_consume(code.code)
    assert second is None, "Already-consumed code must return None"


# ---------------------------------------------------------------------------
# Rate limit tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_max_3_pending_per_platform(store: PairingStore) -> None:
    """Minting a 4th code without consuming any raises PairingPlatformFull."""
    # 3 different users minting on the same platform — all pending
    await store.mint_code(platform="telegram", platform_user_id="user_1")
    await store.mint_code(platform="telegram", platform_user_id="user_2")
    await store.mint_code(platform="telegram", platform_user_id="user_3")

    with pytest.raises(PairingPlatformFull):
        await store.mint_code(platform="telegram", platform_user_id="user_4")


@pytest.mark.asyncio
async def test_max_3_pending_consumed_allows_new(store: PairingStore) -> None:
    """After consuming one, a 4th pending code should succeed."""
    c1 = await store.mint_code(platform="telegram", platform_user_id="u1")
    await store.mint_code(platform="telegram", platform_user_id="u2")
    await store.mint_code(platform="telegram", platform_user_id="u3")

    # Consume one
    await store.verify_and_consume(c1.code)

    # Now a new mint should succeed
    c4 = await store.mint_code(platform="telegram", platform_user_id="u4")
    assert c4 is not None


@pytest.mark.asyncio
async def test_rate_limit_per_user_10min(store: PairingStore) -> None:
    """Same user requesting a 2nd code within 10 minutes raises PairingRateLimited."""
    await store.mint_code(platform="telegram", platform_user_id="alice")

    with pytest.raises(PairingRateLimited):
        await store.mint_code(platform="telegram", platform_user_id="alice")


@pytest.mark.asyncio
async def test_rate_limit_different_users_independent(store: PairingStore) -> None:
    """Different users on the same platform are rate-limited independently."""
    await store.mint_code(platform="telegram", platform_user_id="alice")
    # Bob should not be rate-limited even though Alice is
    code = await store.mint_code(platform="telegram", platform_user_id="bob")
    assert code is not None


@pytest.mark.asyncio
async def test_rate_limit_different_platforms_independent(store: PairingStore) -> None:
    """Same user ID on different platforms has independent rate limits."""
    await store.mint_code(platform="telegram", platform_user_id="alice")
    # Alice on Slack should not be rate-limited
    code = await store.mint_code(platform="slack", platform_user_id="alice")
    assert code is not None


# ---------------------------------------------------------------------------
# Lockout tests (T1.8.5 security test)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_5_failed_approvals_lockout(tmp_path: Path) -> None:
    """5 failed verify_and_consume attempts → 6th mint raises (platform locked).

    The gateway always knows the platform context (the DM channel where the
    user sent the bad code). We pass platform_hint="telegram" to attribute
    failures correctly, matching real gateway usage.
    """
    store = PairingStore(db_path=tmp_path / "p.db")

    # Mint a valid code for this platform (to ensure there's a live platform entry)
    await store.mint_code(platform="telegram", platform_user_id="victim")

    # 5 bad-code attempts with platform attribution (real gateway passes this context)
    for i in range(5):
        result = await store.verify_and_consume(
            f"BADCODE{i}",
            platform_hint="telegram",
        )
        assert result is None

    # Platform should now be locked
    locked = await store.is_platform_locked("telegram")
    assert locked, "Platform must be locked after 5 failed attempts"

    # 6th mint attempt must fail due to lockout
    from arcgateway.pairing import PairingPlatformLocked

    with pytest.raises(PairingPlatformLocked):
        await store.mint_code(platform="telegram", platform_user_id="new_victim")


@pytest.mark.asyncio
async def test_lockout_expires_after_1h(tmp_path: Path) -> None:
    """Lockout expires after 1h; platform becomes usable again."""
    store = PairingStore(db_path=tmp_path / "p.db")

    # Trigger table creation by minting a code first
    await store.mint_code(platform="telegram", platform_user_id="dummy_init")

    # Insert an already-expired lockout directly in DB
    locked_until = time.time() - 1.0  # 1 second in the past
    conn = sqlite3.connect(str(tmp_path / "p.db"))
    conn.execute(
        "INSERT OR REPLACE INTO pairing_lockouts(platform, locked_until) VALUES (?, ?)",
        ("slack", locked_until),
    )
    conn.commit()
    conn.close()

    is_locked = await store.is_platform_locked("slack")
    assert not is_locked, "Expired lockout must not block the platform"


@pytest.mark.asyncio
async def test_is_platform_locked_fresh(store: PairingStore) -> None:
    """A platform with no failures is not locked."""
    locked = await store.is_platform_locked("telegram")
    assert not locked


# ---------------------------------------------------------------------------
# PII / privacy tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_raw_user_id_in_storage(tmp_path: Path) -> None:
    """Raw platform_user_id must never appear in the pairing_codes table."""
    db_path = tmp_path / "p.db"
    store = PairingStore(db_path=db_path)

    raw_user_id = "user_12345_secret"
    await store.mint_code(platform="telegram", platform_user_id=raw_user_id)

    conn = sqlite3.connect(str(db_path))
    rows = conn.execute("SELECT * FROM pairing_codes").fetchall()
    conn.close()

    for row in rows:
        row_str = str(row)
        assert raw_user_id not in row_str, (
            f"Raw user ID found in DB row: {row_str}"
        )


@pytest.mark.asyncio
async def test_code_not_stored_in_audit_columns(tmp_path: Path) -> None:
    """The PairingCode model includes code — but we must never log the raw code.

    This test ensures the code stored in DB under 'code' column is only accessed
    by key, not appearing verbatim in other columns (no duplication leakage).
    """
    db_path = tmp_path / "p.db"
    store = PairingStore(db_path=db_path)
    code_obj = await store.mint_code(platform="telegram", platform_user_id="alice")

    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT platform, user_hash, minted_at, expires_at FROM pairing_codes WHERE code = ?",
        (code_obj.code,),
    ).fetchone()
    conn.close()

    assert row is not None, "Code row must exist"
    # Row columns other than 'code' itself must not contain the raw code
    for val in row:
        assert code_obj.code not in str(val), "Code leaked into non-code column"


# ---------------------------------------------------------------------------
# Cleanup / sweep tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cleanup_expired_sweep(tmp_path: Path) -> None:
    """cleanup_expired removes expired rows; fresh rows survive.

    Uses unique platforms per test to avoid the 3-pending-per-platform cap.
    """
    db_path = tmp_path / "p.db"
    store = PairingStore(db_path=db_path)

    # Mint 5 fresh codes on distinct platforms (1 per platform avoids cap)
    for i in range(5):
        await store.mint_code(platform=f"platform_{i}", platform_user_id="fresh_user")

    # Insert 5 already-expired codes directly via SQL
    conn = sqlite3.connect(str(db_path))
    expired_time = time.time() - 10.0
    import hashlib
    import secrets as secrets_mod

    for i in range(5):
        fake_code = "".join(
            secrets_mod.choice("ABCDEFGHJKLMNPQRSTUVWXYZ23456789") for _ in range(8)
        )
        user_hash = hashlib.sha256(f"expired_user_{i}".encode()).hexdigest()[:16]
        conn.execute(
            """INSERT OR IGNORE INTO pairing_codes
               (code, platform, user_hash, minted_at, expires_at)
               VALUES (?, ?, ?, ?, ?)""",
            (fake_code, "expiredplatform", user_hash, expired_time - 100, expired_time),
        )
    conn.commit()
    conn.close()

    removed = await store.cleanup_expired()
    assert removed == 5, f"Expected 5 expired rows removed, got {removed}"

    # Verify 5 fresh codes remain
    conn = sqlite3.connect(str(db_path))
    remaining = conn.execute("SELECT COUNT(*) FROM pairing_codes").fetchone()[0]
    conn.close()
    assert remaining == 5, f"Expected 5 fresh codes remaining, got {remaining}"


# ---------------------------------------------------------------------------
# list_pending / revoke tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_pending(store: PairingStore) -> None:
    """list_pending returns only unexpired, unconsumed codes."""
    c1 = await store.mint_code(platform="telegram", platform_user_id="u1")
    c2 = await store.mint_code(platform="telegram", platform_user_id="u2")

    # Consume c1
    await store.verify_and_consume(c1.code)

    pending = await store.list_pending()
    codes = [p.code for p in pending]
    assert c2.code in codes
    assert c1.code not in codes


@pytest.mark.asyncio
async def test_revoke(store: PairingStore) -> None:
    """revoke removes a code so verify_and_consume returns None."""
    code = await store.mint_code(platform="telegram", platform_user_id="dave")
    revoked = await store.revoke(code.code)
    assert revoked is True

    result = await store.verify_and_consume(code.code)
    assert result is None, "Revoked code must return None on verify"


@pytest.mark.asyncio
async def test_revoke_nonexistent_returns_false(store: PairingStore) -> None:
    """Revoking a non-existent code returns False without error."""
    result = await store.revoke("NOSUCHCX")
    assert result is False


# ---------------------------------------------------------------------------
# Federal tier tests (T1.8.3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_federal_requires_approver_did(federal_store: PairingStore) -> None:
    """Federal tier: verify_and_consume without approver_did raises PairingSignatureInvalid.

    M3 compliance (T1.8.3): federal tier now raises PairingSignatureInvalid rather than
    returning None so callers cannot silently ignore the missing-approver_did case.
    """
    from arcgateway.pairing import PairingSignatureInvalid
    code = await federal_store.mint_code(
        platform="telegram", platform_user_id="fed_user"
    )
    with pytest.raises(PairingSignatureInvalid):
        await federal_store.verify_and_consume(code.code, approver_did=None)


@pytest.mark.asyncio
async def test_federal_with_approver_did_no_sig_raises(federal_store: PairingStore) -> None:
    """Federal tier: verify_and_consume WITH approver_did but NO signature raises.

    M3 compliance (T1.8.3): federal tier requires a valid Ed25519 signature.
    Providing only approver_did without a signature raises PairingSignatureInvalid.
    """
    from arcgateway.pairing import PairingSignatureInvalid
    code = await federal_store.mint_code(
        platform="telegram", platform_user_id="fed_user_2"
    )
    with pytest.raises(PairingSignatureInvalid):
        await federal_store.verify_and_consume(
            code.code, approver_did="did:arc:operator:alice"
        )


@pytest.mark.asyncio
async def test_non_federal_no_approver_did_succeeds(store: PairingStore) -> None:
    """Non-federal tier: verify_and_consume without approver_did succeeds."""
    code = await store.mint_code(platform="telegram", platform_user_id="regular_user")
    result = await store.verify_and_consume(code.code, approver_did=None)
    assert result is not None


# ---------------------------------------------------------------------------
# PairingCode model tests
# ---------------------------------------------------------------------------


def test_pairing_code_fields() -> None:
    """PairingCode model has required fields."""
    now = time.time()
    pc = PairingCode(
        code="ABCD1234",
        platform="telegram",
        platform_user_id_hash="abc123def456abc1",
        minted_at=now,
        expires_at=now + 3600,
    )
    assert pc.code == "ABCD1234"
    assert pc.platform == "telegram"
    assert len(pc.platform_user_id_hash) == 16
    assert pc.expires_at > pc.minted_at
