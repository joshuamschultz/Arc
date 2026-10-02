"""One resolver for the operator key under every custody (alpha-2 item 70)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest

from arctrust import (
    FileNotaryTransit,
    OperatorKey,
    operator_public_key_for,
    operator_signer_for,
)
from arctrust.keypair import generate_keypair


@dataclass
class _Sec:
    custody: str = "in_process"
    signing_algorithm: str = "ed25519"
    operator_key_dir: str = ""
    notary_keystore: str = ""


def test_in_process_returns_key_file_public_key(tmp_path: Path) -> None:
    key = OperatorKey.load(tmp_path / "operator.key", generate_if_absent=True)
    sec = _Sec(operator_key_dir=str(tmp_path))
    assert operator_public_key_for(sec) == key.public_key


def test_in_process_missing_key_is_none(tmp_path: Path) -> None:
    assert operator_public_key_for(_Sec(operator_key_dir=str(tmp_path))) is None


def test_transit_public_key_wins_over_a_different_key_file(tmp_path: Path) -> None:
    on_disk = OperatorKey.load(tmp_path / "operator.key", generate_if_absent=True)
    vault_seed = generate_keypair().private_key
    FileNotaryTransit.provision(tmp_path / "notary", "operator", vault_seed)
    vault_public = FileNotaryTransit(tmp_path / "notary").public_key("operator")
    assert vault_public != on_disk.public_key
    sec = _Sec(custody="vault_transit", operator_key_dir=str(tmp_path))
    assert operator_public_key_for(sec) == vault_public


def test_transit_signer_is_a_handle_whose_signature_verifies(tmp_path: Path) -> None:
    from arctrust.keypair import verify

    FileNotaryTransit.provision(tmp_path / "notary", "operator", generate_keypair().private_key)
    sec = _Sec(custody="vault_transit", operator_key_dir=str(tmp_path))
    signer = operator_signer_for(sec)
    assert verify(b"m", signer.sign(b"m"), signer.public_key)


def test_unservable_transit_fails_closed_not_to_file(tmp_path: Path) -> None:
    OperatorKey.load(tmp_path / "operator.key", generate_if_absent=True)
    sec = _Sec(custody="vault_transit", operator_key_dir=str(tmp_path))
    with pytest.raises(RuntimeError):
        operator_public_key_for(sec)
