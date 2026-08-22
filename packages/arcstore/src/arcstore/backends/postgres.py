"""Lazy async PostgreSQL backend lifecycle adapter."""

from __future__ import annotations

from importlib import import_module
from typing import Any

from arcstore.config import ArcStoreConfig, PostgresSettings


class PostgresBackend:
    """Owns an asyncpg pool; schema and domain operations are separate work."""

    def __init__(self, settings: ArcStoreConfig | PostgresSettings) -> None:
        self._settings = (
            settings.postgres_settings() if isinstance(settings, ArcStoreConfig) else settings
        )
        self._pool: Any = None

    async def start(self) -> None:
        if self._pool is not None:
            return
        asyncpg = import_module("asyncpg")

        self._pool = await asyncpg.create_pool(
            dsn=self._settings.dsn.get_secret_value(),
            min_size=self._settings.pool_min_size,
            max_size=self._settings.pool_max_size,
            command_timeout=self._settings.command_timeout,
            timeout=self._settings.connect_timeout,
            statement_cache_size=self._settings.statement_cache_size,
            ssl=self._settings.ssl_mode,
        )

    async def stop(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def upsert(self, table: str, key: str, row: dict[str, Any]) -> None:
        raise NotImplementedError("PostgreSQL domain operations are not implemented")

    async def upsert_many(self, table: str, rows: list[tuple[str, dict[str, Any]]]) -> None:
        raise NotImplementedError("PostgreSQL domain operations are not implemented")

    async def query(self, table: str, **kwargs: Any) -> list[dict[str, Any]]:
        raise NotImplementedError("PostgreSQL domain operations are not implemented")

    async def get_cursor(self, name: str) -> int:
        raise NotImplementedError("PostgreSQL domain operations are not implemented")

    async def set_cursor(self, name: str, value: int) -> None:
        raise NotImplementedError("PostgreSQL domain operations are not implemented")
