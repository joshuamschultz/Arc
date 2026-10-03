"""``[security.vault]``: Vault Transit custody in the agent's validated config."""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from arcagent.core.config import SecurityConfig
from arcagent.core.config_loading import apply_env_overrides

VAULT: dict[str, Any] = {
    "addr": "https://vault.internal:8200",
    "ca_bundle": "/etc/arc/vault-ca.pem",
    "role_id": "role",
    "secret_source": "credential:vault-secret-id",
}


def test_vault_block_implies_vault_transit_custody() -> None:
    config = SecurityConfig(vault=VAULT)
    assert config.custody == "vault_transit"
    assert config.vault is not None and config.vault.mount == "transit"


def test_federal_with_vault_signs_ecdsa_p256() -> None:
    config = SecurityConfig(tier="federal", vault=VAULT)
    assert (config.custody, config.signing_algorithm) == ("vault_transit", "ecdsa-p256")


def test_vault_beside_in_process_is_refused() -> None:
    with pytest.raises(ValidationError, match="vault_transit"):
        SecurityConfig(custody="in_process", vault=VAULT)


def test_plain_http_vault_is_refused() -> None:
    with pytest.raises(ValidationError, match="https"):
        SecurityConfig(vault={**VAULT, "addr": "http://vault.internal:8200"})


def test_env_cannot_repoint_the_custody_transit(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARCAGENT_SECURITY__VAULT__ADDR", "https://attacker.example:8200")
    monkeypatch.setenv("ARCAGENT_SECURITY__VAULT__CA_BUNDLE", "/tmp/attacker-ca.pem")
    data = apply_env_overrides({"security": {"vault": dict(VAULT)}})
    assert data["security"]["vault"] == VAULT
