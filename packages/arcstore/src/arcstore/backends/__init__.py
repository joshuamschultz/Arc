"""Storage backends for the arcstore query layer.

``ArcStoreBackend`` is the seam (base.py). ``FakeBackend`` is the in-memory
test double (memory.py).

``open_backend`` is the factory: callers (arcui, ingest, the CLI) select a
backend *by name* and receive a ``StorageBackend``, so the UI read path depends
on the Protocol + this factory rather than a concrete class. Swapping storage is
a config change, not a code edit (SPEC-026 D-009/D-011).
"""

from pydantic import SecretStr

from arcstore.backends.base import (
    AUDIT_TABLE,
    OPERATIONAL_TABLES,
    ArcStoreBackend,
    StorageBackend,
    table_for_kind,
)
from arcstore.backends.memory import FakeBackend
from arcstore.backends.postgres import PostgresBackend


def open_backend(backend: str = "postgres", *, dsn: str | None = None) -> ArcStoreBackend:
    """Return the configured PostgreSQL backend."""
    if backend == "postgres":
        from arcstore.config import ArcStoreConfig

        config = (
            ArcStoreConfig(database_url=SecretStr(dsn)) if dsn is not None else ArcStoreConfig()
        )
        return PostgresBackend(config)
    if backend == "sqlite":
        raise ValueError("SQLite is not a production ArcStore backend")
    raise ValueError(f"Unknown ArcStore backend: {backend!r}")


__all__ = [
    "AUDIT_TABLE",
    "OPERATIONAL_TABLES",
    "ArcStoreBackend",
    "FakeBackend",
    "PostgresBackend",
    "StorageBackend",
    "open_backend",
    "table_for_kind",
]
