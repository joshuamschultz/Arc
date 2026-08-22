"""The production factory has one PostgreSQL result and no SQLite branch."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from arcstore.backends import ArcStoreBackend, PostgresBackend, open_backend
from arcstore.config import ArcStoreConfig


def test_open_backend_returns_the_postgres_contract() -> None:
    backend = open_backend(secret=SecretStr("postgresql://user:secret@localhost/arcstore"))
    assert isinstance(backend, ArcStoreBackend)
    assert isinstance(backend, PostgresBackend)


def test_runtime_config_rejects_persisted_database_url() -> None:
    with pytest.raises(ValueError):
        ArcStoreConfig(database_url="postgresql://user:secret@localhost/arcstore")
