"""The one resolver picks Vault Transit when ``[security.vault]`` is configured."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from packages.arctrust.tests.vault_fake import FakeVault

from arctrust import (
    CredentialCustodyUnavailableError,
    FileNotaryTransit,
    OperatorKey,
    TransitConnectorCipher,
    generate_keypair,
    machine_security,
    operator_signer_for,
    operator_transit_for,
    verify_signature,
)
from arctrust import operator_resolver as resolver
from arctrust.paths import config_file, default_operator_key_path, operator_dir
from arctrust.signer import SignerError
from arctrust.vault_transit import VaultTransit


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    yield
    resolver._VAULT_TRANSITS.clear()


@pytest.fixture
def fake() -> Iterator[FakeVault]:
    with FakeVault() as vault:
        yield vault


def _write(block: str) -> None:
    path = config_file("arcagent.toml")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(block, encoding="utf-8")


def _vault_block(fake: FakeVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    creds = tmp_path / "creds"
    creds.mkdir()
    (creds / "vault-secret-id").write_text(fake.state.secret_id)
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(creds))
    return (
        "[security.vault]\n"
        f'addr = "{fake.addr}"\n'
        f'ca_bundle = "{fake.ca_bundle}"\n'
        f'role_id = "{fake.state.role_id}"\n'
        'secret_source = "credential:vault-secret-id"\n'
        "max_retries = 0\n"
    )


def test_vault_block_implies_vault_transit_custody(
    fake: FakeVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write("[security]\n" + _vault_block(fake, tmp_path, monkeypatch))
    view = machine_security()
    assert view.custody == "vault_transit"
    assert view.vault is not None and view.vault.addr == fake.addr


def test_vault_block_beside_in_process_is_refused(
    fake: FakeVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write('[security]\ncustody = "in_process"\n' + _vault_block(fake, tmp_path, monkeypatch))
    with pytest.raises(ValueError, match="vault_transit"):
        machine_security()


def test_malformed_vault_block_fails_closed() -> None:
    _write('[security]\n[security.vault]\naddr = "http://vault:8200"\n')
    with pytest.raises(ValueError):
        machine_security()


def test_resolver_picks_vault_and_signs_by_reference(
    fake: FakeVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    on_disk = OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    _write("[security]\n" + _vault_block(fake, tmp_path, monkeypatch))
    transit = operator_transit_for(machine_security())
    assert isinstance(transit, VaultTransit)
    assert operator_transit_for(machine_security()) is transit  # one client per process

    signer = operator_signer_for()
    signature = signer.sign(b"checkpoint")
    assert signer.public_key != on_disk.public_key
    assert signer.public_key == bytes(fake.state.keys["arc-operator"].material.verify_key)
    assert verify_signature("ed25519", b"checkpoint", signature, signer.public_key)
    assert fake.calls("/sign/arc-operator") == 1


def test_resolver_without_vault_keeps_the_local_notary() -> None:
    FileNotaryTransit.provision(
        operator_dir() / "notary", "operator", generate_keypair().private_key
    )
    _write('[security]\ncustody = "vault_transit"\n')
    assert isinstance(operator_transit_for(machine_security()), FileNotaryTransit)


def test_unreachable_vault_never_falls_back_to_the_key_file(
    fake: FakeVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    _write("[security]\n" + _vault_block(fake, tmp_path, monkeypatch))
    fake.state.fail_statuses = [503] * 20
    with pytest.raises(SignerError, match="refusing to fall back"):
        operator_signer_for()


def test_connector_cipher_over_vault_maps_outage_to_custody_unavailable(
    fake: FakeVault, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write("[security]\n" + _vault_block(fake, tmp_path, monkeypatch))
    cipher = TransitConnectorCipher(operator_transit_for(machine_security()), require_fips=False)
    sealed = cipher.seal(b"refresh", scope="conn_1", slot="refresh_token")
    assert cipher.open(sealed, scope="conn_1", slot="refresh_token") == b"refresh"
    fake.state.fail_statuses = [503] * 20
    with pytest.raises(CredentialCustodyUnavailableError):
        cipher.open(sealed, scope="conn_1", slot="refresh_token")
