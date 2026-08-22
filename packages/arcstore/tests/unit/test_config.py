"""Unit tests for the single secure ArcStore PostgreSQL configuration."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from arcstore import ArcStoreConfig, resolve_data_dir
from arcstore.backends import open_backend
from arcstore.backends.base import ArcStoreBackend
from arcstore.backends.postgres import PostgresBackend
from arcstore.backends.postgres_inbox import PostgresInboxRepository
from arcstore.config import ENV_DATA_DIR, ENV_DATABASE_URL


def test_config_defaults_and_rejects_unknown_keys() -> None:
    config = ArcStoreConfig()
    assert config.enabled is True
    assert config.pool_min_size == 1
    assert config.pool_max_size == 10
    with pytest.raises(ValidationError):
        ArcStoreConfig(not_a_setting=True)


def test_local_postgres_is_accepted_without_tls_and_external_requires_it() -> None:
    local = ArcStoreConfig()
    assert (
        local.postgres_settings(SecretStr("postgresql://user:secret@localhost/arcstore")).ssl_mode
        == "prefer"
    )
    with pytest.raises(ValueError, match="TLS"):
        ArcStoreConfig().postgres_settings(
            SecretStr("postgresql://user:secret@example.com/arcstore?sslmode=disable")
        )


def test_supabase_pooler_disables_prepared_statement_cache() -> None:
    config = ArcStoreConfig()
    settings = config.postgres_settings(
        SecretStr("postgresql://user:secret@db.pooler.supabase.com:6543/postgres")
    )
    assert settings.statement_cache_size == 0
    assert settings.ssl_mode == "require"
    assert "db.pooler.supabase.com:6543" in settings.dsn.get_secret_value()
    assert "user:secret@" not in repr(config)
    assert "user:secret@" not in repr(settings)


@pytest.mark.asyncio
async def test_supabase_pooler_starts_full_postgres_adapter_with_tls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pooler-safe settings are passed to the same complete PostgresBackend contract."""
    import asyncpg

    calls: dict[str, object] = {}

    class Transaction:
        async def __aenter__(self) -> Transaction:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Connection:
        def transaction(self) -> Transaction:
            return Transaction()

        async def execute(self, *_args: object) -> str:
            return "OK"

        async def fetchval(self, *_args: object) -> None:
            return None

    class Acquire:
        async def __aenter__(self) -> Connection:
            return Connection()

        async def __aexit__(self, *_args: object) -> None:
            return None

    class Pool:
        def acquire(self) -> Acquire:
            return Acquire()

        async def close(self) -> None:
            return None

    async def create_pool(**kwargs: object) -> Pool:
        calls.update(kwargs)
        return Pool()

    monkeypatch.setattr(asyncpg, "create_pool", create_pool)
    dsn = "postgresql://user:secret@db.pooler.supabase.com:6543/postgres"
    backend = open_backend(secret=SecretStr(dsn))
    assert isinstance(backend, PostgresBackend)
    assert isinstance(backend, ArcStoreBackend)
    repository = PostgresInboxRepository(backend)
    assert repository._backend is backend
    await backend.start()
    try:
        assert calls["dsn"] == dsn
        assert calls["ssl"] is True
        assert calls["statement_cache_size"] == 0
    finally:
        await backend.stop()


def test_database_url_env_is_resolved_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(ENV_DATABASE_URL, "postgresql://user:secret@localhost/arcstore")
    assert ArcStoreConfig().postgres_settings().dsn.get_secret_value().endswith("/arcstore")


def test_data_dir_resolution_still_uses_its_single_resolver(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(ENV_DATA_DIR, str(tmp_path / "env-store"))
    assert ArcStoreConfig(data_dir=str(tmp_path / "configured")).resolve_data_dir() == (
        tmp_path / "env-store"
    )
    assert resolve_data_dir() == tmp_path / "env-store"
