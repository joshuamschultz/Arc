"""Unit test conftest.

Pre-imports slack_bolt into sys.modules so that the mock-based
slack adapter tests work correctly.

The test ``test_connect_raises_on_import_error`` uses:
    mods_to_remove = {k: v for k, v in sys.modules.items() if "slack_bolt" in k}
    with patch.dict(sys.modules, {k: None for k in mods_to_remove}):
        ...

This pattern only works when slack_bolt is ALREADY in sys.modules.
If slack_bolt is installed but not yet imported, mods_to_remove is empty,
patch.dict is a no-op, and connect() actually tries a real Slack connection.

Importing slack_bolt here (at conftest load time) ensures it's in sys.modules
before any test in this directory runs.

Signed-pairing helpers
----------------------
After the four-pillar mandate (signature required at all tiers), tests that
exercise non-signature behavior (TTL, collision, consumption, etc.) need to
call verify_and_consume with a valid signature. The ``signed_store`` fixture
and ``make_signed_consume`` helper simplify this for personal-tier tests that
don't want to inline the full Ed25519 signing ceremony.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest
from nacl.signing import SigningKey

from arcgateway.pairing import PairingStore, build_pairing_challenge


def pytest_configure(config: object) -> None:
    """Pre-import slack_bolt so mock-based tests work correctly.

    This matches the pre-existing behavior of the test environment that
    had slack_bolt pre-loaded via the faker plugin or other imports.
    """
    try:
        import slack_bolt  # noqa: F401
    except ImportError:
        pass  # slack_bolt not installed — tests will be skipped or fail naturally


# ---------------------------------------------------------------------------
# Signed-pairing fixtures
# ---------------------------------------------------------------------------

_UNIT_TEST_DID = "did:arc:org:operator/unit-test-operator"


@pytest.fixture
def signing_key() -> SigningKey:
    """Fresh Ed25519 signing key for a single test."""
    return SigningKey.generate()


@pytest.fixture
def signed_store(tmp_path: Path, signing_key: SigningKey) -> PairingStore:
    """Personal-tier PairingStore wired to a local trust dir.

    Use this fixture in tests that need verify_and_consume to succeed without
    testing the signature mechanics themselves. It pre-seeds the operators.toml
    file with the test operator's pubkey so load_operator_pubkey resolves.

    Use ``signed_consume(store, code_obj)`` (see below) to produce a correctly
    signed consume call.
    """
    trust_dir = tmp_path / "trust"
    trust_dir.mkdir()
    pub_b64 = base64.b64encode(bytes(signing_key.verify_key)).decode("ascii")
    operators_file = trust_dir / "operators.toml"
    operators_file.write_text(
        f'[operators."{_UNIT_TEST_DID}"]\npublic_key = "{pub_b64}"\n',
        encoding="utf-8",
    )
    operators_file.chmod(0o600)

    from arctrust import invalidate_cache

    invalidate_cache()

    return PairingStore(
        db_path=tmp_path / "signed.db",
        tier="personal",
        trust_dir=trust_dir,
    )


async def signed_consume(
    store: PairingStore,
    code_obj: Any,
    signing_key: SigningKey,
    approver_did: str = _UNIT_TEST_DID,
) -> Any:
    """Call verify_and_consume with a valid Ed25519 signature.

    Use in tests that need to consume a code but are NOT testing signature
    verification behavior. Produces a correct signature over the challenge so
    the verify path succeeds.

    Args:
        store: PairingStore (must be configured with a trust dir that has
               ``approver_did`` registered).
        code_obj: PairingCode returned by mint_code().
        signing_key: The Ed25519 signing key whose pubkey is registered in the
                     store's trust dir.
        approver_did: DID under which the pubkey is registered.

    Returns:
        Result of verify_and_consume (PairingCode or None).
    """
    challenge = build_pairing_challenge(code_obj.code, code_obj.minted_at)
    sig = bytes(signing_key.sign(challenge).signature)
    return await store.verify_and_consume(
        code_obj.code,
        approver_did=approver_did,
        signature=sig,
    )
