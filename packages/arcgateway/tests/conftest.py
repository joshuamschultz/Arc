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
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture(autouse=True)
def _no_managed_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make it impossible for a test to start or reuse a live broker."""
    import arcteam.nats_server as nats_server

    async def _already_running(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(nats_server, "ensure_nats_server", _already_running)
    consumer = importlib.import_module("arcgateway.broker_bootstrap")
    monkeypatch.setattr(consumer, "ensure_nats_server", _already_running)
