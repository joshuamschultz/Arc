"""The knowledge principal's signing capability (alpha-2 D7).

A connection's shared store belongs to no agent, so its OKF seal is signed by
the connection's own principal ``did:arc:knowledge:<hash>``. The key is held
only by arctrust custody: derived from the operator seed under ``in_process``
custody (zero config), or a provisioned non-exportable transit key under
``vault_transit`` custody, where an unprovisioned key fails closed.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arctrust import FileNotaryTransit, knowledge_signer_for, operator_key_for, verify_signature
from arctrust.knowledge_signer import KNOWLEDGE_KEY_REF
from arctrust.operator_resolver import OPERATOR_KEY_REF, MachineSecurity

_A = "did:arc:knowledge:" + "a" * 32
_B = "did:arc:knowledge:" + "b" * 32


def _in_process(tmp_path: Path) -> MachineSecurity:
    security = MachineSecurity(operator_key_dir=str(tmp_path / "keys"))
    operator_key_for(security, bootstrap=True)
    return security


def test_in_process_signer_is_deterministic_per_principal(tmp_path: Path) -> None:
    security = _in_process(tmp_path)

    first = knowledge_signer_for(_A, security)
    again = knowledge_signer_for(_A, security)
    other = knowledge_signer_for(_B, security)

    assert first is not None and again is not None and other is not None
    assert first.did == _A and first.can_sign
    assert first.public_key == again.public_key, "an existing store keeps its key"
    assert first.public_key != other.public_key, "each connection has its own key"
    signature = first.sign(b"seal")
    assert verify_signature(first.algorithm, b"seal", signature, first.public_key)
    assert not verify_signature(other.algorithm, b"seal", signature, other.public_key)


def test_knowledge_key_is_not_the_operator_key(tmp_path: Path) -> None:
    security = _in_process(tmp_path)
    operator = operator_key_for(security)
    signer = knowledge_signer_for(_A, security)

    assert operator is not None and signer is not None
    assert signer.public_key != operator.public_key


def test_signer_exposes_no_key_material(tmp_path: Path) -> None:
    signer = knowledge_signer_for(_A, _in_process(tmp_path))

    assert signer is not None
    public = {name for name in dir(signer) if not name.startswith("_")}
    assert public == {"algorithm", "can_sign", "did", "public_key", "sign"}


def test_missing_operator_key_fails_closed(tmp_path: Path) -> None:
    security = MachineSecurity(operator_key_dir=str(tmp_path / "absent"))

    assert knowledge_signer_for(_A, security) is None


@pytest.mark.parametrize(
    "principal",
    ["did:arc:agent:x", "did:arc:knowledge:short", "did:arc:knowledge:" + "A" * 32, ""],
)
def test_only_a_knowledge_principal_gets_a_signer(tmp_path: Path, principal: str) -> None:
    with pytest.raises(ValueError):
        knowledge_signer_for(principal, _in_process(tmp_path))


def _transit(tmp_path: Path) -> MachineSecurity:
    keystore = tmp_path / "notary"
    FileNotaryTransit.provision(keystore, OPERATOR_KEY_REF, os.urandom(32))
    return MachineSecurity(custody="vault_transit", notary_keystore=str(keystore))


def test_transit_custody_without_a_provisioned_key_fails_closed(tmp_path: Path) -> None:
    assert knowledge_signer_for(_A, _transit(tmp_path)) is None


def test_transit_custody_signs_by_reference_with_the_provisioned_key(tmp_path: Path) -> None:
    security = _transit(tmp_path)
    FileNotaryTransit.provision(Path(security.notary_keystore), KNOWLEDGE_KEY_REF, os.urandom(32))

    signer = knowledge_signer_for(_A, security)

    assert signer is not None and signer.did == _A
    signature = signer.sign(b"seal")
    assert verify_signature(signer.algorithm, b"seal", signature, signer.public_key)
