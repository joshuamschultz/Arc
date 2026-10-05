"""Adversarial: a forged or replayed authority config or grant never opens the authority.

Run in the cross-package battery (tests/run_adversarial_tests.py). Every case edits
the signed control files an attacker with filesystem write could reach, and expects
the account authority to refuse before any account is read.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from arctrust.authority_config import (
    AuthorityConfigError,
    DeploymentAuthorityConfig,
    sign_authority_config,
)
from arctrust.paths import config_file
from arctrust.vault_lease import (
    Capability,
    CapabilityGrant,
    VaultLeaseError,
    sign_capability_grant,
)
from nacl.signing import SigningKey
from packages.arccli.tests.accounts_support import (
    Deployment,
    RecordingSink,
    enrolled_deployment,
    run_enroll,
)

from arccli.commands._accounts import build_user_store_factory


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Own Arc home per test, even in the cross-package battery (no arccli conftest)."""
    monkeypatch.delenv("ARC_TEAM_ROOT", raising=False)
    monkeypatch.delenv("ARCSTORE_DATA_DIR", raising=False)
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


@pytest.fixture
def deployment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    with enrolled_deployment(tmp_path, monkeypatch) as enrolled:
        yield enrolled


def _open(deployment: Deployment) -> None:
    factory = build_user_store_factory(RecordingSink(), users_path=deployment.users_path)
    assert factory is not None
    factory()


def _write(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload))
    path.chmod(0o600)


def _current_config() -> DeploymentAuthorityConfig:
    raw = json.loads(config_file("accounts-authority.json").read_text())["config"]
    return DeploymentAuthorityConfig.model_validate(raw)


def test_a_config_edited_after_signing_is_refused(deployment):
    path = config_file("accounts-authority.json")
    envelope = json.loads(path.read_text())
    envelope["config"]["vault_url"] = "https://attacker.example"
    _write(path, envelope)
    with pytest.raises(AuthorityConfigError):
        _open(deployment)


def test_a_config_signed_by_a_non_operator_key_is_refused(deployment):
    attacker = SigningKey.generate()
    forged = sign_authority_config(_current_config(), lambda data: attacker.sign(data).signature)
    _write(config_file("accounts-authority.json"), forged)
    with pytest.raises(AuthorityConfigError):
        _open(deployment)


def test_a_replayed_older_config_is_refused_after_rotation(deployment, monkeypatch):
    old = json.loads(config_file("accounts-authority.json").read_text())
    run_enroll(deployment.vault, monkeypatch, "--rotate")
    _write(config_file("accounts-authority.json"), old)
    with pytest.raises(AuthorityConfigError):
        _open(deployment)


def test_a_grant_signed_by_a_non_operator_key_is_refused(deployment):
    attacker = SigningKey.generate()
    config = _current_config()
    forged = {
        capability.value: sign_capability_grant(
            CapabilityGrant(
                deployment_id=config.deployment_id,
                tenant_id=config.tenant_id,
                config_revision=config.revision,
                config_digest=config.digest,
                capability=capability,
                subject="attacker",
                expires_at=int(time.time()) + 3600,
            ),
            lambda data: attacker.sign(data).signature,
        )
        for capability in (Capability.ISSUER, Capability.CIPHER, Capability.ANCHOR)
    }
    _write(config_file("accounts-grants.json"), forged)
    with pytest.raises(VaultLeaseError):
        _open(deployment)


def test_a_grant_for_another_capability_is_refused(deployment):
    path = config_file("accounts-grants.json")
    grants = json.loads(path.read_text())
    grants["issuer"], grants["cipher"] = grants["cipher"], grants["issuer"]
    _write(path, grants)
    with pytest.raises(Exception, match=r"grant|capability"):
        _open(deployment)


def test_a_symlinked_authority_config_is_refused(deployment, tmp_path: Path):
    path = config_file("accounts-authority.json")
    real = tmp_path / "elsewhere.json"
    real.write_text(path.read_text())
    real.chmod(0o600)
    path.unlink()
    path.symlink_to(real)
    with pytest.raises(AuthorityConfigError):
        _open(deployment)
