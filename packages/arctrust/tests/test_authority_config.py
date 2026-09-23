"""A signed config is usable only at its independently anchored revision."""

from __future__ import annotations

import hashlib
import json

import pytest
from nacl.signing import SigningKey

from arctrust.authority_config import (
    AuthorityConfigError,
    DeploymentAuthorityConfig,
    load_authority_config,
    sign_authority_config,
)
from arctrust.monotonic import AnchorHead


class Anchor:
    scope = "arc-acme-dgx-anchor/config"

    def __init__(self, head: AnchorHead | None):
        self.head = head

    def latest(self) -> AnchorHead | None:
        return self.head


def _write(tmp_path, signing_key, *, revision=1):
    config = DeploymentAuthorityConfig(
        deployment_id="dgx", tenant_id="acme", revision=revision,
        vault_url="https://vault.example", vault_ca_sha256="a" * 64,
    )
    envelope = sign_authority_config(config, lambda message: signing_key.sign(message).signature)
    path = tmp_path / "authority.json"
    path.write_text(json.dumps(envelope))
    path.chmod(0o600)
    digest = hashlib.sha256(config.canonical_bytes()).hexdigest()
    anchor = Anchor(AnchorHead(scope=Anchor.scope, version=revision, digest=digest))
    return path, config, anchor


def test_signed_config_requires_independent_key_and_anchor(tmp_path):
    key = SigningKey.generate()
    path, config, anchor = _write(tmp_path, key)
    assert load_authority_config(
        path, trusted_public_key=bytes(key.verify_key),
        expected_deployment="dgx", expected_tenant="acme", anchor=anchor,
    ) == config
    with pytest.raises(AuthorityConfigError):
        load_authority_config(path, trusted_public_key=bytes(SigningKey.generate().verify_key),
                              expected_deployment="dgx", expected_tenant="acme", anchor=anchor)


@pytest.mark.parametrize("change", ["tenant", "deployment", "rollback", "substitution", "unanchored"])
def test_config_refuses_substitution_replay_and_rollback(tmp_path, change):
    key = SigningKey.generate()
    path, _config, anchor = _write(tmp_path, key)
    tenant, deployment = "acme", "dgx"
    if change == "tenant":
        tenant = "other"
    elif change == "deployment":
        deployment = "other"
    elif change == "rollback":
        anchor.head = AnchorHead(scope=anchor.scope, version=2, digest="b" * 64)
    elif change == "substitution":
        payload = json.loads(path.read_text())
        payload["config"]["vault_url"] = "https://attacker.example"
        path.write_text(json.dumps(payload))
    else:
        anchor.head = None
    with pytest.raises(AuthorityConfigError):
        load_authority_config(path, trusted_public_key=bytes(key.verify_key),
                              expected_deployment=deployment, expected_tenant=tenant, anchor=anchor)


def test_namespace_is_derived_and_cannot_be_shared_or_selected(tmp_path):
    key = SigningKey.generate()
    path, config, anchor = _write(tmp_path, key)
    assert config.transit_mount == "arc-acme-dgx-transit"
    assert config.anchor_mount == "arc-acme-dgx-anchor"
    payload = json.loads(path.read_text())
    payload["config"]["transit_mount"] = "shared-transit"
    path.write_text(json.dumps(payload))
    with pytest.raises(AuthorityConfigError):
        load_authority_config(path, trusted_public_key=bytes(key.verify_key),
                              expected_deployment="dgx", expected_tenant="acme", anchor=anchor)
