"""The runner host signs through the operator signer HANDLE, never a key file."""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust import FileNotaryTransit, generate_keypair
from arctrust.keypair import verify
from arctrust.paths import config_file, operator_dir

from arcgateway.workflow_runner_host import _operator_signer


def test_runner_host_builds_signer_from_handle_under_transit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
    config = config_file("arcagent.toml")
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text('[security]\ncustody = "vault_transit"\n', encoding="utf-8")
    # No operator.key anywhere: only the notary holds the key.
    FileNotaryTransit.provision(
        operator_dir() / "notary", "operator", generate_keypair().private_key
    )

    signer = _operator_signer()

    assert not (operator_dir() / "operator.key").exists()
    assert verify(b"msg", signer.sign(b"msg"), signer.public_key)
