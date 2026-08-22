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
from arctrust.paths import ARC_CONFIG_DIR_ENV


@pytest.fixture
def arcstore_backend() -> object:
    """Return the per-test in-memory ArcStore backend."""
    from arcstore.backends.memory import FakeBackend

    return FakeBackend()


@pytest.fixture(autouse=True)
def _isolated_arcstore_backend(monkeypatch: pytest.MonkeyPatch, arcstore_backend: object) -> None:
    """Make every implicit ArcUI composition use a fresh contract-complete backend.

    Production requires PostgreSQL and the CLI validates that requirement before
    constructing the app. Unit and route tests that do not explicitly inject a
    backend must remain hermetic rather than inheriting a developer DSN.
    """

    def open_fake(**_kwargs: object) -> object:
        return arcstore_backend

    monkeypatch.setattr("arcstore.backends.open_backend", open_fake)
    monkeypatch.setattr("arcui.observe.open_backend", open_fake)
    monkeypatch.setattr("arcui.server.open_backend", open_fake)


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


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relocate the Arc home so no test can reach the developer's real ``~/.arc``.

    Without this, a test that resolves any :mod:`arctrust.paths` accessor writes
    into the invoking user's own deployment: this suite was observed rewriting
    the real viewer token and APPENDING to the real operator-signed WORM chain.
    Both are live state a developer depends on, and the WORM chain additionally
    enforces a single writer, so a polluting test also fails outright beside any
    concurrent run — diagnosing the machine rather than the code.

    Autouse and set before every other fixture body, so a test that names its own
    ``ARC_CONFIG_DIR`` still wins (its ``setenv`` lands after this one).
    """
    monkeypatch.setenv(ARC_CONFIG_DIR_ENV, str(tmp_path / "arc-home"))
