"""Tests for the agent-lifecycle arcstore spin-up (SPEC-026 FR-6).

Covers AC-6.2 (StoreIngest started and stopped cleanly, no orphan task) and
AC-6.3 (broken backend → agent continues, spool still works, degraded).
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from arccli.commands.agent import _store_lifecycle as sl
from arcstore import ArcStoreConfig


def _cfg(tmp_path: Path, **kw: object) -> ArcStoreConfig:
    return ArcStoreConfig(data_dir=str(tmp_path), **kw)  # type: ignore[arg-type]


# -- load_arcstore_config -----------------------------------------------------

def test_load_config_defaults_when_no_toml(tmp_path: Path) -> None:
    cfg = sl.load_arcstore_config(tmp_path)
    assert cfg.enabled is True
    assert cfg.backend == "sqlite"


def test_load_config_reads_arcstore_block(tmp_path: Path) -> None:
    (tmp_path / "arcagent.toml").write_text(
        '[arcstore]\nenabled = false\nbackend = "sqlite"\n'
    )
    cfg = sl.load_arcstore_config(tmp_path)
    assert cfg.enabled is False


def test_load_config_falls_back_on_malformed_block(tmp_path: Path) -> None:
    (tmp_path / "arcagent.toml").write_text("[arcstore]\nsample_rate = 99.0\n")
    cfg = sl.load_arcstore_config(tmp_path)  # 99.0 > 1.0 is invalid → defaults
    assert cfg.sample_rate == 1.0


# -- managed_store_ingest -----------------------------------------------------

def test_spool_dir_always_created_even_when_disabled(tmp_path: Path) -> None:
    async def _go() -> None:
        async with sl.managed_store_ingest(_cfg(tmp_path, enabled=False)) as ingest:
            assert ingest is None
    asyncio.run(_go())
    assert (tmp_path / "spool").is_dir()


def test_enabled_starts_and_stops_ingest_no_orphan(tmp_path: Path) -> None:
    async def _go() -> int:
        async with sl.managed_store_ingest(_cfg(tmp_path)) as ingest:
            assert ingest is not None
        # After exit, no arcstore ingest task should be left running.
        return sum(
            1
            for t in asyncio.all_tasks()
            if t is not asyncio.current_task() and not t.done()
        )

    leftover = asyncio.run(_go())
    assert leftover == 0
    assert (tmp_path / "store").is_dir()


def test_broken_backend_is_fail_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backend that cannot start must not stop the agent; spool still works."""
    import arcstore.backends as backends

    def _boom(*_a: object, **_k: object) -> object:
        raise RuntimeError("backend unavailable")

    monkeypatch.setattr(backends, "open_backend", _boom)

    async def _go() -> object:
        async with sl.managed_store_ingest(_cfg(tmp_path)) as ingest:
            return ingest  # degraded → None, but no raise

    result = asyncio.run(_go())
    assert result is None
    # Spool path still exists → call-now-see-later guarantee holds.
    assert (tmp_path / "spool").is_dir()
