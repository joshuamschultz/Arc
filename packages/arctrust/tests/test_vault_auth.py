"""Bootstrap-secret sources: never in config, read once, never through a symlink."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from arctrust.vault_auth import VaultSecretSourceError, read_secret_source


def test_systemd_credential_is_read_from_credentials_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "vault-secret-id").write_text("s3cret\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    assert read_secret_source("credential:vault-secret-id") == "s3cret"


def test_credential_without_credentials_directory_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    with pytest.raises(VaultSecretSourceError):
        read_secret_source("credential:vault-secret-id")


def test_credential_name_cannot_escape_the_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path / "creds"))
    (tmp_path / "outside").write_text("x")
    with pytest.raises(VaultSecretSourceError):
        read_secret_source("credential:../outside")


def test_file_descriptor_is_read_to_end_and_closed() -> None:
    read_end, write_end = os.pipe()
    os.write(write_end, b"from-fd\n")
    os.close(write_end)
    assert read_secret_source(f"fd:{read_end}") == "from-fd"
    with pytest.raises(OSError):
        os.fstat(read_end)


def test_env_secret_is_popped(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ARC_VAULT_SECRET_ID", "from-env")
    assert read_secret_source("env:ARC_VAULT_SECRET_ID") == "from-env"
    assert "ARC_VAULT_SECRET_ID" not in os.environ


def test_symlinked_file_is_refused(tmp_path: Path) -> None:
    (tmp_path / "real").write_text("token")
    (tmp_path / "link").symlink_to(tmp_path / "real")
    assert read_secret_source(f"file:{tmp_path / 'real'}") == "token"
    with pytest.raises(VaultSecretSourceError):
        read_secret_source(f"file:{tmp_path / 'link'}")


@pytest.mark.parametrize(
    "source", ["s3cret-inline", "file:relative/path", "fd:abc", "env:lower", "env:EMPTY_X"]
)
def test_malformed_or_empty_sources_are_refused(source: str) -> None:
    with pytest.raises(VaultSecretSourceError):
        read_secret_source(source)
