"""Tests for resolve_secret at personal tier.

Personal tier contract:
- vault configured + found → return vault value
- vault None + env set → return env var
- vault None + env missing + file exists (0600) → return file content
- vault None + env missing + no file → RuntimeError
- vault raises VaultUnreachable → continue to env/file
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.modules.vault.protocol import VaultUnreachable
from arcagent.modules.vault.resolver import resolve_secret


class _FoundBackend:
    def __init__(self, value: str) -> None:
        self._value = value

    async def get_secret(self, path: str) -> str | None:
        return self._value


class _NoneBackend:
    async def get_secret(self, path: str) -> str | None:
        return None


class _UnreachableBackend:
    async def get_secret(self, path: str) -> str | None:
        raise VaultUnreachable("personal vault down")


@pytest.mark.asyncio
async def test_personal_vault_found_returns_value() -> None:
    backend = _FoundBackend("vault-val")
    result = await resolve_secret("s", tier="personal", backend=backend)
    assert result == "vault-val"


@pytest.mark.asyncio
async def test_personal_vault_none_env_set_returns_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_SECRET", "env-val")
    result = await resolve_secret(
        "my-secret",
        tier="personal",
        backend=_NoneBackend(),
        env_fallback_var="MY_SECRET",
    )
    assert result == "env-val"


@pytest.mark.asyncio
async def test_personal_no_backend_env_set_returns_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_SECRET", "env-only")
    result = await resolve_secret(
        "my-secret",
        tier="personal",
        backend=None,
        env_fallback_var="MY_SECRET",
    )
    assert result == "env-only"


@pytest.mark.asyncio
async def test_personal_vault_unreachable_falls_back_to_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MY_SECRET", "env-fallback")
    result = await resolve_secret(
        "my-secret",
        tier="personal",
        backend=_UnreachableBackend(),
        env_fallback_var="MY_SECRET",
    )
    assert result == "env-fallback"


@pytest.mark.asyncio
async def test_personal_file_fallback_returns_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """File at 0600 mode is read as last fallback."""
    monkeypatch.delenv("MY_SECRET", raising=False)

    secrets_dir = tmp_path / "secrets"
    secrets_dir.mkdir()
    secret_file = secrets_dir / "my-secret"
    secret_file.write_text("file-secret-value")
    secret_file.chmod(0o600)

    # Patch FileBackend's default dir to use tmp_path
    import arcagent.modules.vault.backends.file as file_mod

    original_default = file_mod._DEFAULT_SECRETS_DIR
    file_mod._DEFAULT_SECRETS_DIR = secrets_dir
    try:
        result = await resolve_secret(
            "my-secret",
            tier="personal",
            backend=None,
            env_fallback_var="MY_SECRET",
        )
    finally:
        file_mod._DEFAULT_SECRETS_DIR = original_default

    assert result == "file-secret-value"


@pytest.mark.asyncio
async def test_personal_all_sources_missing_raises(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """No vault, no env, no file → RuntimeError."""
    monkeypatch.delenv("MY_SECRET", raising=False)

    import arcagent.modules.vault.backends.file as file_mod

    original_default = file_mod._DEFAULT_SECRETS_DIR
    file_mod._DEFAULT_SECRETS_DIR = tmp_path / "nonexistent"
    try:
        with pytest.raises(RuntimeError, match="personal"):
            await resolve_secret(
                "my-secret",
                tier="personal",
                backend=None,
                env_fallback_var="MY_SECRET",
            )
    finally:
        file_mod._DEFAULT_SECRETS_DIR = original_default


@pytest.mark.asyncio
async def test_invalid_tier_raises_value_error() -> None:
    with pytest.raises(ValueError, match="Invalid tier"):
        await resolve_secret("s", tier="invalid", backend=None)


@pytest.mark.asyncio
async def test_empty_name_raises_value_error() -> None:
    with pytest.raises(ValueError, match="empty"):
        await resolve_secret("", tier="personal", backend=None)
