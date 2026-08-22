"""Storage backends for the arcstore query layer.

``StorageBackend`` is the seam (base.py). ``SqliteBackend`` is the default
(sqlite.py). ``FakeBackend`` is the in-memory test double (memory.py) that
proves no SQLite type leaks into the Protocol.

``open_backend`` is the factory: callers (arcui, ingest, the CLI) select a
backend *by name* and receive a ``StorageBackend``, so the UI read path depends
on the Protocol + this factory rather than a concrete class. Swapping storage is
a config change, not a code edit (SPEC-026 D-009/D-011).
"""

from pathlib import Path

from arcstore.backends.base import (
    AUDIT_TABLE,
    OPERATIONAL_TABLES,
    StorageBackend,
    table_for_kind,
)
from arcstore.backends.memory import FakeBackend
from arcstore.backends.sqlite import SqliteBackend

# Backends not yet implemented are deferred behind the Protocol (D-009): a clear
# error beats silently falling back to SQLite under a Postgres config.
_DEFERRED = frozenset({"postgres", "cloud"})


def open_backend(backend: str = "sqlite", db_path: Path | str = "") -> StorageBackend:
    """Return a ``StorageBackend`` for the named backend.

    ``sqlite`` is the only concrete backend today; ``postgres``/``cloud`` raise
    ``NotImplementedError`` (deferred behind the Protocol). An unknown name is a
    ``ValueError`` at the config boundary.
    """
    if backend == "sqlite":
        return SqliteBackend(Path(db_path))
    if backend in _DEFERRED:
        raise NotImplementedError(
            f"arcstore backend {backend!r} is deferred behind the StorageBackend "
            "Protocol (SPEC-026 D-009); only 'sqlite' is implemented."
        )
    raise ValueError(f"Unknown arcstore backend: {backend!r}. Use 'sqlite'.")


__all__ = [
    "AUDIT_TABLE",
    "OPERATIONAL_TABLES",
    "FakeBackend",
    "SqliteBackend",
    "StorageBackend",
    "open_backend",
    "table_for_kind",
]
