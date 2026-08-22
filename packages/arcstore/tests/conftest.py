"""Shared integration fixtures for an explicitly provisioned PostgreSQL test database."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from pydantic import SecretStr

from arcstore.backends.base import ArcStoreBackend
from arcstore.backends.postgres import PostgresBackend
from arcstore.config import ArcStoreConfig


@pytest.fixture
async def postgres_backend() -> AsyncIterator[ArcStoreBackend]:
    """Provide a migrated real backend only when the operator explicitly configured it."""
    dsn = os.environ.get("ARCSTORE_TEST_DATABASE_URL")
    if not dsn:
        pytest.skip("ARCSTORE_TEST_DATABASE_URL is required for PostgreSQL integration tests")
    backend = PostgresBackend(ArcStoreConfig().postgres_settings(SecretStr(dsn)))
    await backend.start()
    try:
        yield backend
    finally:
        await backend.stop()
