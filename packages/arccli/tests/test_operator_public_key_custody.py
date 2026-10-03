"""``operator_public_key`` and arctrust's machine view agree with SecurityConfig."""

from __future__ import annotations

from pathlib import Path

import arcagent
import pytest
from arctrust import FileNotaryTransit, OperatorKey, generate_keypair, machine_security
from arctrust.paths import config_file, default_operator_key_path, operator_dir

from arccli.commands.operator import operator_public_key


def _write_security(block: str) -> None:
    config = config_file("arcagent.toml")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(f"[security]\n{block}\n", encoding="utf-8")


@pytest.fixture(autouse=True)
def _home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


def test_vault_held_key_differs_from_disk_file_and_wins() -> None:
    on_disk = OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    FileNotaryTransit.provision(
        operator_dir() / "notary", "operator", generate_keypair().private_key
    )
    _write_security('custody = "vault_transit"')

    pinned = operator_public_key()

    assert pinned is not None
    assert pinned != on_disk.public_key


def test_in_process_still_reads_the_key_file() -> None:
    key = OperatorKey.load(default_operator_key_path(), generate_if_absent=True)
    assert operator_public_key() == key.public_key


@pytest.mark.parametrize(
    "block",
    [
        "",
        'tier = "enterprise"',
        'tier = "enterprise"\ncustody = "in_process"',
        'tier = "federal"',
    ],
)
def test_machine_view_matches_security_config_custody(block: str) -> None:
    _write_security(block)
    view = machine_security()
    config = arcagent.SecurityConfig(
        **{k: v.strip('"') for k, v in (line.split(" = ") for line in block.splitlines())}
    )
    assert (view.custody, view.signing_algorithm) == (config.custody, config.signing_algorithm)


@pytest.mark.parametrize("custody", ["", 'custody = "vault_transit"\n', 'tier = "federal"\n'])
def test_vault_block_agrees_with_security_config(custody: str, tmp_path: Path) -> None:
    import tomllib

    block = (
        f"{custody}[security.vault]\n"
        'addr = "https://vault.internal:8200"\n'
        f'ca_bundle = "{tmp_path / "ca.pem"}"\n'
        'role_id = "r"\n'
        'secret_source = "credential:vault-secret-id"\n'
    )
    _write_security(block)
    view = machine_security()
    config = arcagent.SecurityConfig(**tomllib.loads(f"[security]\n{block}")["security"])
    assert view.custody == config.custody == "vault_transit"
    assert view.vault == config.vault


def test_vault_block_beside_in_process_is_refused_by_both(tmp_path: Path) -> None:
    import tomllib

    block = (
        'custody = "in_process"\n[security.vault]\naddr = "https://v:8200"\n'
        f'ca_bundle = "{tmp_path / "ca.pem"}"\nrole_id = "r"\nsecret_source = "env:X"\n'
    )
    _write_security(block)
    with pytest.raises(ValueError, match="vault_transit"):
        machine_security()
    with pytest.raises(ValueError, match="vault_transit"):
        arcagent.SecurityConfig(**tomllib.loads(f"[security]\n{block}")["security"])
