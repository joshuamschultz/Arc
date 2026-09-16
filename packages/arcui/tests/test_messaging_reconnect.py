"""The embedded NATS connection must survive an overnight broker blip.

The dashboard builds its team-messaging backend once, at lifespan startup, and
caches the handle on ``app.state.messaging_service``. Nothing rebuilds it. So a
connection opened with ``allow_reconnect=False`` is killed permanently by the
first disconnect — a broker restart, an idle timeout, any blip — and every
channel route answers ``team_messaging_unavailable`` until arcui is restarted by
hand. This is the "unavailable every morning" failure.

The connection is long-lived, so it must reconnect indefinitely. Startup still
fails fast because :func:`_preflight` guards the initial connect; reconnect only
governs behaviour *after* a healthy connection has already been established.
"""

from __future__ import annotations

from typing import Any

import pytest

import arcui.messaging as messaging


class _FakeNC:
    def jetstream(self) -> object:
        return object()


@pytest.mark.asyncio
async def test_connect_backend_reconnects_indefinitely(monkeypatch: Any) -> None:
    captured: dict[str, Any] = {}

    async def _fake_preflight(_url: str) -> None:
        return None

    async def _fake_connect(_url: str, **kwargs: Any) -> _FakeNC:
        captured.update(kwargs)
        return _FakeNC()

    monkeypatch.setattr(messaging, "_preflight", _fake_preflight)
    import nats

    monkeypatch.setattr(nats, "connect", _fake_connect)

    backend = await messaging._connect_backend()

    assert backend is not None
    assert captured["allow_reconnect"] is True, (
        "a long-lived dashboard connection must reconnect after a broker blip"
    )
    assert captured["max_reconnect_attempts"] == -1, (
        "reconnect must never give up — the dashboard runs for days"
    )
