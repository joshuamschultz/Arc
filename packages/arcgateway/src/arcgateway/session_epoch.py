"""SessionEpochStore — per-session generation counter for session rotation.

A session is identified by a deterministic key derived from (agent, user)
(see ``session.build_session_key``). To let an operator start a *fresh*
conversation without losing the ability to resume the old one, we fold a
monotonic **generation** into that key: rotating bumps the generation, and a
new generation hashes to a new key, which ``SessionManager.open_or_resume``
opens as an empty session. No file is ever reset — minting a new key *is* the
reset.

This store maps an opaque base key (the generation-0 digest) -> generation.
It is deliberately agnostic about how the key is built so it carries no raw
DIDs (federal privacy posture) and has no import cycle with ``session``.

Design constraints:
    * ``generation()`` is called from ``SessionRouter.handle`` *before* the
      synchronous race guard, where no ``await`` may occur. Every method here
      is synchronous and backed by an in-memory read-through cache, so the hot
      path never blocks on disk.
    * Persistence is optional. ``db_path=None`` keeps state in memory only
      (tests, ephemeral runs). A path persists generations across restarts —
      otherwise "New session" would silently un-rotate on the next bounce.
    * ``SessionRouter`` is single-threaded asyncio, so the read-modify-write in
      ``bump`` is never interrupted; the durable write uses an atomic upsert.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS session_epochs (
    key TEXT PRIMARY KEY,
    generation INTEGER NOT NULL
);
"""


class SessionEpochStore:
    """Maps an opaque base session key to its current generation counter."""

    def __init__(self, db_path: Path | None = None) -> None:
        """Initialise the store.

        Args:
            db_path: SQLite file backing persistence. ``None`` keeps
                generations in memory only (no cross-restart survival).
        """
        self._db_path = db_path
        self._cache: dict[str, int] = {}
        if db_path is not None:
            self._init_db(db_path)
            self._cache = self._load_cache(db_path)

    def generation(self, base_key: str) -> int:
        """Return the current generation for ``base_key`` (0 if never bumped)."""
        return self._cache.get(base_key, 0)

    def bump(self, base_key: str) -> int:
        """Increment and return the generation for ``base_key``.

        Synchronous read-modify-write; safe under single-threaded asyncio.
        """
        new_gen = self._cache.get(base_key, 0) + 1
        self._cache[base_key] = new_gen
        if self._db_path is not None:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.execute(
                    "INSERT INTO session_epochs (key, generation) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET generation = generation + 1",
                    (base_key, new_gen),
                )
        return new_gen

    @staticmethod
    def _init_db(db_path: Path) -> None:
        """Create the DB file and schema (idempotent)."""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(str(db_path)) as conn:
            conn.executescript(_SCHEMA_SQL)

    @staticmethod
    def _load_cache(db_path: Path) -> dict[str, int]:
        """Read all generations from disk into a cache dict."""
        with sqlite3.connect(str(db_path)) as conn:
            rows = conn.execute("SELECT key, generation FROM session_epochs").fetchall()
        return {str(key): int(gen) for key, gen in rows}
