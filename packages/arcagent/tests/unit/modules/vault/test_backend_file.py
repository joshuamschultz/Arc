"""Tests for FileBackend — including 0600 enforcement."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from arcagent.modules.vault.backends.file import FileBackend


@pytest.fixture
def secrets_dir(tmp_path: Path) -> Path:
    d = tmp_path / "secrets"
    d.mkdir()
    return d


@pytest.mark.asyncio
async def test_reads_secret_at_0600_mode(secrets_dir: Path) -> None:
    """File with mode 0600 is read correctly."""
    secret_file = secrets_dir / "my-secret"
    secret_file.write_text("the-secret-value")
    secret_file.chmod(0o600)

    backend = FileBackend(secrets_dir=secrets_dir)
    result = await backend.get_secret("my-secret")
    assert result == "the-secret-value"


@pytest.mark.asyncio
async def test_refuses_file_at_0644_mode(secrets_dir: Path) -> None:
    """File with mode 0644 is REFUSED — security gate."""
    secret_file = secrets_dir / "insecure-secret"
    secret_file.write_text("should-not-read")
    secret_file.chmod(0o644)

    backend = FileBackend(secrets_dir=secrets_dir)
    result = await backend.get_secret("insecure-secret")
    assert result is None


@pytest.mark.asyncio
async def test_refuses_file_at_0640_mode(secrets_dir: Path) -> None:
    """File with mode 0640 (group-readable) is also refused."""
    secret_file = secrets_dir / "group-readable"
    secret_file.write_text("do-not-read")
    secret_file.chmod(0o640)

    backend = FileBackend(secrets_dir=secrets_dir)
    result = await backend.get_secret("group-readable")
    assert result is None


@pytest.mark.asyncio
async def test_refuses_file_at_0666_mode(secrets_dir: Path) -> None:
    """World-readable file is refused."""
    secret_file = secrets_dir / "world-readable"
    secret_file.write_text("do-not-read")
    secret_file.chmod(0o666)

    backend = FileBackend(secrets_dir=secrets_dir)
    result = await backend.get_secret("world-readable")
    assert result is None


@pytest.mark.asyncio
async def test_returns_none_for_missing_file(secrets_dir: Path) -> None:
    backend = FileBackend(secrets_dir=secrets_dir)
    result = await backend.get_secret("nonexistent-secret")
    assert result is None


@pytest.mark.asyncio
async def test_strips_whitespace_from_file_content(secrets_dir: Path) -> None:
    secret_file = secrets_dir / "whitespace-secret"
    secret_file.write_text("  secret-value\n")
    secret_file.chmod(0o600)

    backend = FileBackend(secrets_dir=secrets_dir)
    result = await backend.get_secret("whitespace-secret")
    assert result == "secret-value"


@pytest.mark.asyncio
async def test_empty_file_returns_none(secrets_dir: Path) -> None:
    secret_file = secrets_dir / "empty-secret"
    secret_file.write_text("")
    secret_file.chmod(0o600)

    backend = FileBackend(secrets_dir=secrets_dir)
    result = await backend.get_secret("empty-secret")
    assert result is None


@pytest.mark.asyncio
async def test_rejects_path_traversal(secrets_dir: Path) -> None:
    """Path traversal attempts (e.g., '../etc/passwd') are rejected."""
    backend = FileBackend(secrets_dir=secrets_dir)
    result = await backend.get_secret("../etc/passwd")
    assert result is None


@pytest.mark.asyncio
async def test_rejects_slash_in_name(secrets_dir: Path) -> None:
    """Secret names with slashes are rejected."""
    backend = FileBackend(secrets_dir=secrets_dir)
    result = await backend.get_secret("nested/secret")
    assert result is None


@pytest.mark.asyncio
async def test_default_secrets_dir_is_home_arc_secrets() -> None:
    """Default secrets_dir is ~/.arc/secrets (may not exist — just verify path)."""
    backend = FileBackend()
    expected = Path("~/.arc/secrets").expanduser()
    assert backend._secrets_dir == expected
