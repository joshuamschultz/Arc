"""Ed25519 signature verification for DM pairing approvals (M3 gap-close).

Covers the core federal signature contract:

- A valid Ed25519 signature over ``sha256(code + minted_at_iso)`` approves.
- ``signed_by_did`` is persisted on the consumed row.
- ``gateway.pairing.signature_verified`` audit event is emitted on success.
- A bad signature raises ``PairingSignatureInvalid`` AND records a failure
  against the platform lockout counter (brute-force defence).
- ``gateway.pairing.signature_invalid`` audit event is emitted on failure.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from arcgateway.pairing import (
    PairingSignatureInvalid,
    PairingStore,
    build_pairing_challenge,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def trust_dir(tmp_path: Path) -> Path:
    """Trust dir seeded for the default operator DID used in these tests."""
    d = tmp_path / "trust"
    d.mkdir()
    return d


@pytest.fixture
def operator_did() -> str:
    return "did:arc:org:operator/alice001"


@pytest.fixture
def operator_key() -> SigningKey:
    # Deterministic-ish test key; SigningKey.generate is fine — we bind pubkey below.
    return SigningKey.generate()


@pytest.fixture
def federal_store_with_trust(
    tmp_path: Path,
    trust_dir: Path,
    operator_did: str,
    operator_key: SigningKey,
) -> PairingStore:
    """Federal PairingStore wired to a populated trust dir."""
    pub_b64 = base64.b64encode(bytes(operator_key.verify_key)).decode("ascii")
    operators_file = trust_dir / "operators.toml"
    operators_file.write_text(
        f'[operators."{operator_did}"]\npublic_key = "{pub_b64}"\n',
        encoding="utf-8",
    )
    operators_file.chmod(0o600)

    # Trust-store cache is module-level; flush to avoid test interference.
    from arctrust import invalidate_cache

    invalidate_cache()

    return PairingStore(
        db_path=tmp_path / "fed.db",
        tier="federal",
        trust_dir=trust_dir,
    )


# ---------------------------------------------------------------------------
# Happy path: valid signature
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_valid_signature_approves(
    federal_store_with_trust: PairingStore,
    operator_did: str,
    operator_key: SigningKey,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Valid Ed25519 signature over the challenge → approval succeeds."""
    code = await federal_store_with_trust.mint_code(
        platform="telegram", platform_user_id="alice_target"
    )

    challenge = build_pairing_challenge(code.code, code.minted_at)
    signature = operator_key.sign(challenge).signature

    caplog.set_level(logging.INFO, logger="arcgateway.pairing.audit")
    result = await federal_store_with_trust.verify_and_consume(
        code.code,
        approver_did=operator_did,
        signature=signature,
    )

    assert result is not None
    assert result.code == code.code
    assert result.signed_by_did == operator_did

    # signature_verified audit emitted
    verified_msgs = [r for r in caplog.records if "signature_verified" in r.getMessage()]
    assert verified_msgs, "Expected gateway.pairing.signature_verified audit event"


@pytest.mark.asyncio
async def test_signed_by_did_persisted_on_row(
    tmp_path: Path,
    federal_store_with_trust: PairingStore,
    operator_did: str,
    operator_key: SigningKey,
) -> None:
    """After approval, the DB row carries signed_by_did == approver_did."""
    code = await federal_store_with_trust.mint_code(
        platform="slack", platform_user_id="alice_on_slack"
    )
    challenge = build_pairing_challenge(code.code, code.minted_at)
    sig = operator_key.sign(challenge).signature

    await federal_store_with_trust.verify_and_consume(
        code.code, approver_did=operator_did, signature=sig
    )

    db_path = tmp_path / "fed.db"
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT signed_by_did FROM pairing_codes WHERE code = ?", (code.code,)
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == operator_did


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_invalid_signature_raises_and_does_not_consume(
    federal_store_with_trust: PairingStore,
    operator_did: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bad signature → PairingSignatureInvalid, code remains unconsumed."""
    code = await federal_store_with_trust.mint_code(
        platform="telegram", platform_user_id="attack_target"
    )

    bogus_sig = b"\x00" * 64

    caplog.set_level(logging.INFO, logger="arcgateway.pairing.audit")
    with pytest.raises(PairingSignatureInvalid):
        await federal_store_with_trust.verify_and_consume(
            code.code,
            approver_did=operator_did,
            signature=bogus_sig,
        )

    # Code row should still be unconsumed — failed approval does not consume.
    db_path = tmp_path / "fed.db"
    conn = sqlite3.connect(str(db_path))
    consumed = conn.execute(
        "SELECT consumed FROM pairing_codes WHERE code = ?", (code.code,)
    ).fetchone()[0]
    conn.close()
    assert consumed == 0

    invalid_msgs = [r for r in caplog.records if "signature_invalid" in r.getMessage()]
    assert invalid_msgs, "Expected gateway.pairing.signature_invalid audit event"


@pytest.mark.asyncio
async def test_wrong_key_signature_raises(
    federal_store_with_trust: PairingStore,
    operator_did: str,
) -> None:
    """Signature from a different (non-trusted) key does not verify."""
    code = await federal_store_with_trust.mint_code(
        platform="telegram", platform_user_id="attack_target_2"
    )
    wrong_key = SigningKey.generate()
    challenge = build_pairing_challenge(code.code, code.minted_at)
    sig = wrong_key.sign(challenge).signature

    with pytest.raises(PairingSignatureInvalid):
        await federal_store_with_trust.verify_and_consume(
            code.code, approver_did=operator_did, signature=sig
        )


@pytest.mark.asyncio
async def test_tampered_code_fails_verification(
    federal_store_with_trust: PairingStore,
    operator_did: str,
    operator_key: SigningKey,
) -> None:
    """Signature bound to code A cannot approve code B (challenge mismatch)."""
    code_a = await federal_store_with_trust.mint_code(platform="telegram", platform_user_id="uA")
    code_b = await federal_store_with_trust.mint_code(platform="telegram", platform_user_id="uB")

    # Sign challenge for code A
    challenge_a = build_pairing_challenge(code_a.code, code_a.minted_at)
    sig_a = operator_key.sign(challenge_a).signature

    # Try to use it against code B
    with pytest.raises(PairingSignatureInvalid):
        await federal_store_with_trust.verify_and_consume(
            code_b.code, approver_did=operator_did, signature=sig_a
        )


@pytest.mark.asyncio
async def test_signature_failure_increments_lockout_counter(
    federal_store_with_trust: PairingStore,
    operator_did: str,
) -> None:
    """5 bad-signature attempts against the same code → platform locked.

    Using the same mint deliberately: a failed approval does NOT consume the
    code, so the same row can be hit 5 times.  This also proves that bogus
    signatures don't count against per-user rate limits or the per-platform
    pending cap (both of which would trip on 5 fresh mints).
    """
    code = await federal_store_with_trust.mint_code(platform="telegram", platform_user_id="victim")

    for _ in range(5):
        with pytest.raises(PairingSignatureInvalid):
            await federal_store_with_trust.verify_and_consume(
                code.code,
                approver_did=operator_did,
                signature=b"\x00" * 64,
            )

    assert await federal_store_with_trust.is_platform_locked("telegram")


# ---------------------------------------------------------------------------
# Unknown operator DID
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_operator_did_in_trust_store_raises(
    tmp_path: Path,
    trust_dir: Path,
    operator_key: SigningKey,
) -> None:
    """DID not in operators.toml → PairingSignatureInvalid (federal hard-fail)."""
    # Trust dir is empty: no operators.toml entries
    operators_file = trust_dir / "operators.toml"
    operators_file.write_text("[operators]\n", encoding="utf-8")
    operators_file.chmod(0o600)

    from arctrust import invalidate_cache

    invalidate_cache()

    store = PairingStore(
        db_path=tmp_path / "fed.db",
        tier="federal",
        trust_dir=trust_dir,
    )

    code = await store.mint_code(platform="slack", platform_user_id="u1")
    challenge = build_pairing_challenge(code.code, code.minted_at)
    sig = operator_key.sign(challenge).signature

    with pytest.raises(PairingSignatureInvalid):
        await store.verify_and_consume(
            code.code,
            approver_did="did:arc:org:operator/unknown",
            signature=sig,
        )
