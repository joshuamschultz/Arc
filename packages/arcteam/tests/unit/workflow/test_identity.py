"""COMP-011 — the runner's own actor identity (REQ-232).

Distinct from any agent identity AND from the dashboard's shared operator
identity, resolved from disk, never generated.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import OperatorKey
from arctrust.identity import did_from_public_key

from arcteam.workflow.identity import RunnerIdentity, RunnerIdentityUnavailableError


def _write_key(tmp_path: Path) -> OperatorKey:
    key = OperatorKey.generate()
    key.save(tmp_path / "operator" / "operator.key")
    return key


def test_a_missing_key_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(RunnerIdentityUnavailableError):
        RunnerIdentity.load(tmp_path / "operator" / "operator.key")


def test_a_missing_key_is_never_generated(tmp_path: Path) -> None:
    key_path = tmp_path / "operator" / "operator.key"
    with pytest.raises(RunnerIdentityUnavailableError):
        RunnerIdentity.load(key_path)
    assert not key_path.exists(), "the runner never mints an identity for itself"


def test_the_did_derives_from_the_on_disk_key(tmp_path: Path) -> None:
    key = _write_key(tmp_path)

    identity = RunnerIdentity.load(tmp_path / "operator" / "operator.key")

    assert identity.did == did_from_public_key(
        key.public_key, org="local", agent_type="workflow-runner"
    )
    assert identity.public_key_hex == key.public_key.hex()


def test_the_runner_identity_is_not_the_dashboard_operator(tmp_path: Path) -> None:
    key = _write_key(tmp_path)
    identity = RunnerIdentity.load(tmp_path / "operator" / "operator.key")

    assert identity.did != "did:arc:ui:operator"
    assert identity.did != did_from_public_key(
        key.public_key, org="local", agent_type="operator"
    )
    assert "workflow-runner" in identity.did


def test_the_identity_can_sign_its_narration(tmp_path: Path) -> None:
    _write_key(tmp_path)
    identity = RunnerIdentity.load(tmp_path / "operator" / "operator.key")

    signer = identity.message_signer()

    assert signer.did == identity.did
