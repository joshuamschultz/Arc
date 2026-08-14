"""Shared arcgateway test fixtures.

COMP-008 makes the composition root ensure a message broker on every launch
path, which is the point of REQ-306 — and it means any test that builds the
embedded gateway would otherwise spawn a real ``nats-server`` against the
developer's own JetStream store, or bind to whatever is listening on the
deployment port. Tests must not do either, so the ensure call is replaced with
the answer a broker that is *already running* gives: reachable, nothing for the
caller to own, no process and no socket.

The replacement is installed at the canonical module path AND on the consumer
module that binds the name at import time, because patching only one of the two
is what lets a real call slip through in a fresh environment.

The same reasoning applies to the Arc home itself, so it is relocated here too.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
from arctrust.paths import ARC_CONFIG_DIR_ENV


@pytest.fixture(autouse=True)
def _isolated_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Relocate the Arc home so no test can reach the developer's real ``~/.arc``.

    A gateway test that builds an ArcAgent gets that agent's WORM audit chain at
    ``store_dir()/worm/audit-chain-<agent>.jsonl``, which without this points
    into the invoking user's own deployment: the test both pollutes real audit
    state and, because ``WormSink`` enforces a single writer per chain, fails
    outright whenever a second process holds the same file. A test that passes
    alone and errors beside a concurrent run is diagnosing the developer's
    machine, not the code.

    Set before every other fixture body that wants its own root, so a test
    naming an explicit ``ARC_CONFIG_DIR`` still wins.
    """
    monkeypatch.setenv(ARC_CONFIG_DIR_ENV, str(tmp_path / "arc-home"))


@pytest.fixture(autouse=True)
def _no_managed_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make it impossible for a test to start or reuse a live broker."""
    import arcteam.nats_server as nats_server

    async def _already_running(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(nats_server, "ensure_nats_server", _already_running)
    consumer = importlib.import_module("arcgateway.broker_bootstrap")
    monkeypatch.setattr(consumer, "ensure_nats_server", _already_running)
