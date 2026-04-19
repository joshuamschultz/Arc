"""Enterprise-tier signature semantics.

Contract:

- Missing signature: WARN audit emitted; approval PROCEEDS (no raise).
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

    from arcagent.core.trust_store import invalidate_cache

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
async def test_enterprise_missing_signature_warns_and_allows(
    enterprise_store: PairingStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Enterprise tier: missing signature emits warn audit but approval succeeds."""
    code = await enterprise_store.mint_code(
        platform="telegram", platform_user_id="ent_user"
    )

    caplog.set_level(logging.INFO, logger="arcgateway.pairing.audit")
    result = await enterprise_store.verify_and_consume(
        code.code,
        approver_did="did:arc:org:operator/entbody",
        signature=None,
    )
    assert result is not None, "Enterprise tier must still approve when sig absent"

    # signature_missing audit event emitted
    msgs = [r.getMessage() for r in caplog.records]
    assert any("signature_missing" in m for m in msgs), (
        f"Expected signature_missing audit event, got: {msgs}"
    )


@pytest.mark.asyncio
async def test_enterprise_invalid_signature_rejects(
    enterprise_store: PairingStore,
) -> None:
    """Enterprise tier: present-but-bad signature → PairingSignatureInvalid (fail-closed)."""
    code = await enterprise_store.mint_code(
        platform="slack", platform_user_id="ent_user_2"
    )

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
    code = await enterprise_store.mint_code(
        platform="discord", platform_user_id="ent_user_3"
    )

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
async def test_personal_tier_ignores_signatures(tmp_path: Path) -> None:
    """Personal tier: signature is neither required nor validated."""
    store = PairingStore(db_path=tmp_path / "personal.db", tier="personal")
    code = await store.mint_code(platform="telegram", platform_user_id="home_user")

    # Bogus signature and unknown DID — personal tier still accepts
    result = await store.verify_and_consume(
        code.code,
        approver_did="did:arc:whatever/hahaha",
        signature=b"\x00" * 64,
    )
    assert result is not None
