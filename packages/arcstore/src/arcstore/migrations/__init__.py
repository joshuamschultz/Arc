"""Packaged, forward-only ArcStore PostgreSQL schema migrations."""

from __future__ import annotations

from importlib.resources import files
from typing import Any

SCHEMA_HEAD = 6


async def migrate(connection: Any) -> None:
    """Apply the current schema idempotently inside the caller's transaction."""
    await connection.execute(
        "CREATE TABLE IF NOT EXISTS arcstore_schema (version integer PRIMARY KEY)"
    )
    version = await connection.fetchval("SELECT max(version) FROM arcstore_schema")
    if version is not None and version > SCHEMA_HEAD:
        raise RuntimeError("ArcStore database schema is newer than this runtime")
    for migration_version in range((version or 0) + 1, SCHEMA_HEAD + 1):
        sql = files(__package__).joinpath(f"v{migration_version}.sql").read_text(encoding="utf-8")
        await connection.execute(sql)
        await connection.execute(
            "INSERT INTO arcstore_schema(version) VALUES ($1)", migration_version
        )
