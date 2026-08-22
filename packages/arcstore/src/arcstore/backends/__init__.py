"""The one asynchronous PostgreSQL ArcStore backend and its test fake."""

from __future__ import annotations

from pydantic import SecretStr

from arcstore.backends.base import (
    APPROVAL_OUTBOX_TABLE,
    AUDIT_TABLE,
    OPERATIONAL_TABLES,
    ArcStoreBackend,
    StorageBackend,
    table_for_kind,
)
from arcstore.backends.memory import FakeBackend
from arcstore.backends.postgres import PostgresBackend
from arcstore.config import ArcStoreConfig


def open_backend(
    *, config: ArcStoreConfig | None = None, secret: SecretStr | None = None
) -> ArcStoreBackend:
    """Create the production backend from an environment or vault-injected secret."""
    config = ArcStoreConfig() if config is None else config
    return PostgresBackend(config.postgres_settings(secret))


__all__ = [
    "APPROVAL_OUTBOX_TABLE",
    "AUDIT_TABLE",
    "OPERATIONAL_TABLES",
    "ArcStoreBackend",
    "FakeBackend",
    "PostgresBackend",
    "StorageBackend",
    "open_backend",
    "table_for_kind",
]
