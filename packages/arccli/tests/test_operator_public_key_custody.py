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
