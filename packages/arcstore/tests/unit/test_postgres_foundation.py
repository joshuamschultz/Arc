from __future__ import annotations

import sys
import types

import pytest

from arcstore.backends import ArcStoreBackend, PostgresBackend, open_backend
from arcstore.config import ArcStoreConfig


def test_postgres_is_default_and_sqlite_is_rejected() -> None:
    backend = open_backend(dsn="postgresql://user:secret@localhost/arc")
    assert isinstance(backend, PostgresBackend)
    assert isinstance(backend, ArcStoreBackend)
    with pytest.raises(ValueError, match="SQLite"):
        open_backend("sqlite")


def test_settings_redact_dsn_and_disable_prepared_statements_for_supabase_pooler() -> None:
    config = ArcStoreConfig(
        database_url="postgresql://user:secret@db.pooler.supabase.com:6543/postgres"
    )
    settings = config.postgres_settings()
    assert settings.statement_cache_size == 0
    assert "secret" not in repr(config)
    assert "secret" not in repr(settings)


def test_external_tls_cannot_be_disabled() -> None:
    with pytest.raises(ValueError, match="TLS"):
        ArcStoreConfig(
            database_url="postgresql://u:p@example.com/db?sslmode=disable"
        ).postgres_settings()


@pytest.mark.asyncio
async def test_start_is_lazy_and_configures_asyncpg_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: dict[str, object] = {}

    async def create_pool(**kwargs: object) -> object:
        calls.update(kwargs)

        async def close() -> None:
            return None

        return types.SimpleNamespace(close=close)

    monkeypatch.setitem(sys.modules, "asyncpg", types.SimpleNamespace(create_pool=create_pool))
    backend = open_backend(dsn="postgresql://u:p@localhost/db")
    await backend.start()
    assert calls["statement_cache_size"] == 100
    assert calls["ssl"] == "require"
    await backend.stop()
