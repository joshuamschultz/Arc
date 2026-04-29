"""Enterprise-tier signature semantics.

Contract (four-pillar mandate):

- Missing signature: PairingSignatureInvalid raised (all tiers require sig).
- Present but invalid signature: PairingSignatureInvalid (fail-closed).
- Present and valid signature: approval succeeds; signature_verified audit emitted.
"""

from __future__ import annotations

import base64
import logging
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from arcgateway.pairing import (
    PairingSignatureInvalid,
    PairingStore,
    build_pairing_challenge,
)


@pytest.fixture
def trust_dir(tmp_path: Path) -> Path:
    d = tmp_path / "trust"
    d.mkdir()
    return d


@pytest.fixture
def enterprise_store(tmp_path: Path, trust_dir: Path) -> PairingStore:
    # Seed a known operator so the present-signature path can verify
    did = "did:arc:org:operator/entbody"
    sk = SigningKey.generate()
    pub_b64 = base64.b64encode(bytes(sk.verify_key)).decode("ascii")
    (trust_dir / "operators.toml").write_text(
        f'[operators."{did}"]\npublic_key = "{pub_b64}"\n',
        encoding="utf-8",
    )
    (trust_dir / "operators.toml").chmod(0o600)

    from arctrust import invalidate_cache

    invalidate_cache()

    store = PairingStore(
        db_path=tmp_path / "ent.db",
        tier="enterprise",
        trust_dir=trust_dir,
    )
    # Stash the key on the store for tests to pick up
    store._test_sk = sk  # type: ignore[attr-defined]
    store._test_did = did  # type: ignore[attr-defined]
    return store


@pytest.mark.asyncio
async def test_enterprise_missing_signature_raises(
    enterprise_store: PairingStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enterprise tier: missing signature → PairingSignatureInvalid (all tiers require sig)."""
    code = await enterprise_store.mint_code(platform="telegram", platform_user_id="ent_user")

    caplog.set_level(logging.INFO, logger="arcgateway.pairing.audit")
    with pytest.raises(PairingSignatureInvalid):
        await enterprise_store.verify_and_consume(
            code.code,
            approver_did="did:arc:org:operator/entbody",
            signature=None,
        )

    # signature_invalid audit event emitted before raising
    msgs = [r.getMessage() for r in caplog.records]
    assert any("signature_invalid" in m for m in msgs), (
        f"Expected signature_invalid audit event, got: {msgs}"
    )


@pytest.mark.asyncio
async def test_enterprise_invalid_signature_rejects(
    enterprise_store: PairingStore,
) -> None:
    """Enterprise tier: present-but-bad signature → PairingSignatureInvalid (fail-closed)."""
    code = await enterprise_store.mint_code(platform="slack", platform_user_id="ent_user_2")

    with pytest.raises(PairingSignatureInvalid):
        await enterprise_store.verify_and_consume(
            code.code,
            approver_did="did:arc:org:operator/entbody",
            signature=b"\x00" * 64,
        )


@pytest.mark.asyncio
async def test_enterprise_valid_signature_verifies(
    enterprise_store: PairingStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enterprise tier: valid signature → signature_verified audit emitted."""
    code = await enterprise_store.mint_code(platform="discord", platform_user_id="ent_user_3")

    sk = enterprise_store._test_sk  # type: ignore[attr-defined]
    did = enterprise_store._test_did  # type: ignore[attr-defined]
    challenge = build_pairing_challenge(code.code, code.minted_at)
    sig = sk.sign(challenge).signature

    caplog.set_level(logging.INFO, logger="arcgateway.pairing.audit")
    result = await enterprise_store.verify_and_consume(
        code.code,
        approver_did=did,
        signature=sig,
    )
    assert result is not None
    assert result.signed_by_did == did

    msgs = [r.getMessage() for r in caplog.records]
    assert any("signature_verified" in m for m in msgs)


@pytest.mark.asyncio
async def test_personal_tier_requires_signature(tmp_path: Path) -> None:
    """Personal tier: signature is required (self-signed key accepted as trust anchor).

    Bogus/missing signatures are rejected — personal tier is not a bypass.
    """
    store = PairingStore(db_path=tmp_path / "personal.db", tier="personal")
    code = await store.mint_code(platform="telegram", platform_user_id="home_user")

    # No signature → raise
    with pytest.raises(PairingSignatureInvalid):
        await store.verify_and_consume(
            code.code,
            approver_did="did:arc:whatever/hahaha",
            signature=None,
        )

    # Bogus 64-byte signature → raise (nacl verify failure)
    # Re-mint because the previous failed attempt increments the failure counter.
    # (Revoked code still returns None, so use a fresh platform user.)
    code2 = await store.mint_code(platform="slack", platform_user_id="home_user2")
    with pytest.raises(PairingSignatureInvalid):
        await store.verify_and_consume(
            code2.code,
            approver_did="did:arc:whatever/hahaha",
            signature=b"\x00" * 64,
        )
