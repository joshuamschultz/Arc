"""Unit tests for SessionEpochStore — per-(agent,user) session generation.

The store maps an opaque base session key -> generation counter. Rotating a
session bumps the counter; folding the counter into build_session_key mints a
fresh, empty session. These tests pin: default 0, monotonic bump, persistence
across store instances (survives restart), and in-memory mode with no db_path.
"""

from __future__ import annotations

from pathlib import Path

from arcgateway.session_epoch import SessionEpochStore

_KEY = "abc123def4567890"
_OTHER = "0987654321fedcba"


def test_generation_defaults_to_zero_for_unknown_key() -> None:
    store = SessionEpochStore()
    assert store.generation(_KEY) == 0


def test_bump_increments_and_returns_new_generation() -> None:
    store = SessionEpochStore()
    assert store.bump(_KEY) == 1
    assert store.generation(_KEY) == 1
    assert store.bump(_KEY) == 2
    assert store.generation(_KEY) == 2


def test_bump_is_per_key_isolated() -> None:
    store = SessionEpochStore()
    store.bump(_KEY)
    assert store.generation(_KEY) == 1
    assert store.generation(_OTHER) == 0


def test_in_memory_store_does_not_persist_across_instances() -> None:
    a = SessionEpochStore()  # db_path=None -> in-memory only
    a.bump(_KEY)
    b = SessionEpochStore()
    assert b.generation(_KEY) == 0


def test_persisted_store_survives_restart(tmp_path: Path) -> None:
    db = tmp_path / "session_epochs.db"
    a = SessionEpochStore(db_path=db)
    assert a.bump(_KEY) == 1
    assert a.bump(_KEY) == 2

    # A fresh store over the same file (a server restart) sees the bumped gen.
    b = SessionEpochStore(db_path=db)
    assert b.generation(_KEY) == 2
    assert b.bump(_KEY) == 3


def test_persisted_store_creates_parent_dir(tmp_path: Path) -> None:
    db = tmp_path / "nested" / "dir" / "epochs.db"
    store = SessionEpochStore(db_path=db)
    store.bump(_KEY)
    assert db.exists()
