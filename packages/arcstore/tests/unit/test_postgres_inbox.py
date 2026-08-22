"""Construction checks for the PostgreSQL InboxRepository adapter."""

from __future__ import annotations

from arcstore.backends.postgres import PostgresBackend
from arcstore.backends.postgres_inbox import PostgresInboxRepository
from arcstore.inbox import InboxRepository


def test_postgres_inbox_repository_uses_the_existing_backend_lifecycle() -> None:
    backend = PostgresBackend.__new__(PostgresBackend)
    repository = PostgresInboxRepository(backend)
    assert repository._backend is backend
    assert isinstance(repository, InboxRepository)
