"""Shared arcui test fixtures.

Isolate every test from the developer's real Arc data dir: the server now runs
an arcstore ``StoreIngest`` (Observe plane, SPEC-026 FR-5) in its lifespan, so
without this an in-process ``TestClient`` would backfill/write into
``~/.arc/store``. Pointing ``ARCSTORE_DATA_DIR`` at a per-test tmp dir keeps the
observability mirror hermetic.

The same reasoning covers the broker: the lifespan hosts the embedded gateway,
which ensures one on startup (COMP-008 / REQ-306), so an unguarded test would
spawn a real ``nats-server`` against the developer's JetStream store.
"""

from __future__ import annotations

import importlib
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


@pytest.fixture(autouse=True)
def _no_managed_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Answer as an already-running broker would: no process, no socket, nothing owned.

    Patched at the canonical path AND on the consumer module that binds the name
    at import time — patching one of the two is what lets a real call through.
    """
    import arcteam.nats_server as nats_server

    async def _already_running(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(nats_server, "ensure_nats_server", _already_running)
    consumer = importlib.import_module("arcgateway.broker_bootstrap")
    monkeypatch.setattr(consumer, "ensure_nats_server", _already_running)
