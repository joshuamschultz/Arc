"""Federal-tier signature requirement tests.

Specifically covers the contract:

    At federal tier, approval REQUIRES a non-None ``signature`` argument.

- Missing signature → PairingSignatureInvalid raised (hard fail).
- Approval row is NOT consumed when signature is missing.
- A failure is recorded against the platform lockout counter.
- Audit event ``gateway.pairing.signature_invalid`` is emitted with
  ``reason=missing_signature``.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
from pathlib import Path

import pytest
from nacl.signing import SigningKey

from arcgateway.pairing import PairingSignatureInvalid, PairingStore


@pytest.fixture
def trust_dir(tmp_path: Path) -> Path:
    d = tmp_path / "trust"
    d.mkdir()
    return d


@pytest.fixture
def federal_store(tmp_path: Path, trust_dir: Path) -> PairingStore:
    # Seed an operator so the DID-resolution path isn't what fails first
    did = "did:arc:org:operator/fedbody"
    sk = SigningKey.generate()
    pub_b64 = base64.b64encode(bytes(sk.verify_key)).decode("ascii")
    (trust_dir / "operators.toml").write_text(
        f'[operators."{did}"]\npublic_key = "{pub_b64}"\n',
        encoding="utf-8",
    )
    (trust_dir / "operators.toml").chmod(0o600)

    from arcagent.core.trust_store import invalidate_cache

    invalidate_cache()

    return PairingStore(
        db_path=tmp_path / "fed.db",
        tier="federal",
        trust_dir=trust_dir,
    )


@pytest.mark.asyncio
async def test_federal_missing_signature_raises(
    federal_store: PairingStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Federal: approver_did supplied but signature=None → PairingSignatureInvalid."""
    code = await federal_store.mint_code(
        platform="telegram", platform_user_id="victim"
    )

    caplog.set_level(logging.INFO, logger="arcgateway.pairing.audit")
    with pytest.raises(PairingSignatureInvalid):
        await federal_store.verify_and_consume(
            code.code,
            approver_did="did:arc:org:operator/fedbody",
            signature=None,
        )

    invalid_msgs = [
        r.getMessage()
        for r in caplog.records
        if "signature_invalid" in r.getMessage()
    ]
    assert invalid_msgs
    assert any("missing_signature" in m for m in invalid_msgs)


@pytest.mark.asyncio
async def test_federal_missing_signature_does_not_consume(
    federal_store: PairingStore, tmp_path: Path
) -> None:
    """Row must remain unconsumed after a hard-fail sig-missing approval attempt."""
    code = await federal_store.mint_code(
        platform="slack", platform_user_id="victim2"
    )

    with pytest.raises(PairingSignatureInvalid):
        await federal_store.verify_and_consume(
            code.code,
            approver_did="did:arc:org:operator/fedbody",
            signature=None,
        )

    conn = sqlite3.connect(str(tmp_path / "fed.db"))
    row = conn.execute(
        "SELECT consumed, signed_by_did FROM pairing_codes WHERE code = ?",
        (code.code,),
    ).fetchone()
    conn.close()
    assert row is not None
    assert row[0] == 0, "Row must not be consumed on failed federal approval"
    assert row[1] is None


@pytest.mark.asyncio
async def test_federal_missing_signature_counts_as_failure(
    federal_store: PairingStore,
) -> None:
    """5 missing-signature approvals against the same code → platform lockout.

    Failed approvals do not consume the code, so repeated attempts on the
    same row correctly exercise the per-platform failure counter without
    running into the per-user 10-min rate limit or the 3-pending cap.
    """
    code = await federal_store.mint_code(
        platform="telegram", platform_user_id="fedvic"
    )
    for _ in range(5):
        with pytest.raises(PairingSignatureInvalid):
            await federal_store.verify_and_consume(
                code.code,
                approver_did="did:arc:org:operator/fedbody",
                signature=None,
            )

    assert await federal_store.is_platform_locked("telegram")


@pytest.mark.asyncio
async def test_federal_missing_approver_did_raises_and_audits(
    federal_store: PairingStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Federal + missing approver_did is a hard failure with audit + lockout counter.

    Post-SPEC-018 Wave 2 security close: what used to silently return None
    now emits a ``gateway.pairing.signature_invalid`` audit with
    ``reason=missing_approver_did`` and records a platform failure so a
    no-DID approval attempt cannot be used to probe for valid codes
    without tripping the lockout counter.
    """
    code = await federal_store.mint_code(
        platform="discord", platform_user_id="victim3"
    )
    caplog.set_level(logging.INFO, logger="arcgateway.pairing.audit")
    with pytest.raises(PairingSignatureInvalid, match="federal tier requires"):
        await federal_store.verify_and_consume(
            code.code,
            approver_did=None,
            signature=None,
            platform_hint="discord",
        )

    invalid_msgs = [
        r.getMessage()
        for r in caplog.records
        if "signature_invalid" in r.getMessage()
    ]
    assert invalid_msgs, "expected a signature_invalid audit event"
    assert any("missing_approver_did" in m for m in invalid_msgs), (
        "expected reason=missing_approver_did in audit event"
    )


@pytest.mark.asyncio
async def test_federal_missing_approver_did_increments_lockout_counter(
    federal_store: PairingStore,
) -> None:
    """5 no-DID approvals → platform lockout (the failure counter must tick)."""
    for i in range(5):
        code = await federal_store.mint_code(
            platform="slack", platform_user_id=f"probe_{i}"
        )
        with pytest.raises(PairingSignatureInvalid):
            await federal_store.verify_and_consume(
                code.code,
                approver_did=None,
                signature=None,
                platform_hint="slack",
            )
    assert await federal_store.is_platform_locked("slack")
