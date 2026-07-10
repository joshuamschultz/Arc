"""``arc gateway pair approve/list/revoke`` — CLI wiring to the live PairingStore.

Root cause fixed here: PairingStore.verify_and_consume() requires a valid
Ed25519 signature at EVERY tier (arcgateway.pairing_signature — "four-pillar
mandate", not a federal-only rule). The original `_gateway_pair_approve_handler`
called `store.verify_and_consume(code)` with no approver_did/signature at all,
so it unconditionally raised PairingSignatureInvalid — `arc gateway pair
approve` could never succeed, at any tier. These tests drive the full,
correct flow: `arc identity init` registers a self-signed trust anchor, then
the approve handler signs the challenge with that identity before consuming.

These tests also prove approve/list/revoke resolve `[pairing].db_path` from
GatewayConfig (ARC_CONFIG_DIR-aware discovery) rather than PairingStore's own
hardcoded default — the second half of "approve must take effect on the live
gateway": if the CLI and the daemon don't agree on db_path, nothing works.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arccli.commands.identity import identity_handler
from arccli.commands.registry import (
    _gateway_pair_approve_handler,
    _gateway_pair_list_handler,
    _gateway_pair_revoke_handler,
)


def _write_gateway_toml(tmp_path: Path, db_path: Path) -> None:
    (tmp_path / "gateway.toml").write_text(
        f"""\
[gateway]
tier = "personal"

[pairing]
db_path = "{db_path}"
""",
        encoding="utf-8",
    )


@pytest.fixture(autouse=True)
def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this file runs under an isolated ARC_CONFIG_DIR."""
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path))


def test_approve_succeeds_after_identity_init(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The full, real flow: identity init -> mint -> approve -> is_approved."""
    import asyncio

    from arcgateway.pairing import PairingStore

    db_path = tmp_path / "pairing.db"
    _write_gateway_toml(tmp_path, db_path)
    identity_handler(["init"])

    store = PairingStore(db_path=db_path, tier="personal")
    code = asyncio.run(store.mint_code(platform="telegram", platform_user_id="alice"))

    _gateway_pair_approve_handler([code.code])

    out = capsys.readouterr().out
    assert "Approved" in out
    assert asyncio.run(store.is_approved("telegram", "alice")) is True


def test_approve_without_signing_authority_gives_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No `arc identity init` yet -> actionable error, not a stack trace."""
    import asyncio

    from arcgateway.pairing import PairingStore

    db_path = tmp_path / "pairing.db"
    _write_gateway_toml(tmp_path, db_path)
    # No identity_handler(["init"]) call — no signing authority exists.

    store = PairingStore(db_path=db_path, tier="personal")
    code = asyncio.run(store.mint_code(platform="telegram", platform_user_id="bob"))

    with pytest.raises(SystemExit):
        _gateway_pair_approve_handler([code.code])

    err = capsys.readouterr().err
    assert "arc identity init" in err


def test_approve_invalid_code_gives_clear_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = tmp_path / "pairing.db"
    _write_gateway_toml(tmp_path, db_path)
    identity_handler(["init"])

    with pytest.raises(SystemExit):
        _gateway_pair_approve_handler(["NOSUCHCX"])

    err = capsys.readouterr().err
    assert "invalid" in err.lower() or "not found" in err.lower()


def test_approve_uses_configured_db_path_not_pairing_store_default(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A non-default [pairing].db_path is honored, not PairingStore()'s own default.

    This is the cross-process contract this whole fix exists for: the live
    gateway daemon and this CLI invocation must read/write the SAME SQLite
    file, which only happens if both resolve db_path from GatewayConfig.
    """
    import asyncio

    from arcgateway.pairing import PairingStore

    custom_db_path = tmp_path / "custom_location" / "pairing.db"
    _write_gateway_toml(tmp_path, custom_db_path)
    identity_handler(["init"])

    store = PairingStore(db_path=custom_db_path, tier="personal")
    code = asyncio.run(store.mint_code(platform="telegram", platform_user_id="carol"))

    _gateway_pair_approve_handler([code.code])

    assert asyncio.run(store.is_approved("telegram", "carol")) is True


def test_list_uses_configured_db_path(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    import asyncio

    from arcgateway.pairing import PairingStore

    db_path = tmp_path / "pairing.db"
    _write_gateway_toml(tmp_path, db_path)

    store = PairingStore(db_path=db_path, tier="personal")
    code = asyncio.run(store.mint_code(platform="telegram", platform_user_id="dave"))

    _gateway_pair_list_handler([])

    out = capsys.readouterr().out
    assert code.code in out


def test_revoke_uses_configured_db_path(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    import asyncio

    from arcgateway.pairing import PairingStore

    db_path = tmp_path / "pairing.db"
    _write_gateway_toml(tmp_path, db_path)

    store = PairingStore(db_path=db_path, tier="personal")
    code = asyncio.run(store.mint_code(platform="telegram", platform_user_id="erin"))

    _gateway_pair_revoke_handler([code.code])

    out = capsys.readouterr().out
    assert "Revoked" in out
    result = asyncio.run(store.verify_and_consume(code.code))
    assert result is None
