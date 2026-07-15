"""Shared arcagent test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolate_arcstore_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the shared arcstore data dir at a per-test tmp dir.

    The policy WORM chain now defaults into ``<data_dir>/worm/`` (so the arcui
    Security-screen ingest tails it). Without isolation, every full-startup test
    would open a ``WormSink`` on the real ``~/.arc/store/worm/audit-chain-<ws>.jsonl``
    and collide across tests on the single-writer flock. Env wins in
    ``resolve_data_dir`` precedence, so any test that also reads/writes the store
    stays self-consistent under this same dir.
    """
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arcstore"))
