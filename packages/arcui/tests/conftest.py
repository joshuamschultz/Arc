"""Shared arcui test fixtures.

Isolate every test from the developer's real Arc data dir: the server now runs
an arcstore ``StoreIngest`` (Observe plane, SPEC-026 FR-5) in its lifespan, so
without this an in-process ``TestClient`` would backfill/write into
``~/.arc/store``. Pointing ``ARCSTORE_DATA_DIR`` at a per-test tmp dir keeps the
observability mirror hermetic.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _isolated_arc_data_dir(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    data_dir = tmp_path_factory.mktemp("arcdata")
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(data_dir))
    yield data_dir
