"""``open_backend`` factory — the seam that lets callers (arcui, ingest, CLI)
select a storage backend by name without importing a concrete class.

SPEC-026 D-009/D-011: SqliteBackend is the only concrete backend today;
Postgres/cloud are deferred behind the Protocol. The factory is what keeps the
UI read path backend-agnostic — it depends on the Protocol + this factory, not
on ``SqliteBackend`` directly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcstore.backends import SqliteBackend, StorageBackend, open_backend


def test_open_backend_sqlite_returns_protocol(tmp_path: Path) -> None:
    backend = open_backend("sqlite", tmp_path / "x.db")
    assert isinstance(backend, StorageBackend)
    assert isinstance(backend, SqliteBackend)


def test_open_backend_default_is_sqlite(tmp_path: Path) -> None:
    assert isinstance(open_backend(db_path=tmp_path / "x.db"), SqliteBackend)


def test_open_backend_deferred_backend_raises(tmp_path: Path) -> None:
    with pytest.raises(NotImplementedError):
        open_backend("postgres", tmp_path / "x.db")


def test_open_backend_unknown_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        open_backend("bogus", tmp_path / "x.db")
