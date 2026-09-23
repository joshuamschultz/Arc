"""Messaging lifecycle refuses partial setup and recovers a failed start."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from arcui import messaging_lifecycle as lifecycle


class _Backend:
    available = True

    def __init__(self) -> None:
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _app() -> Any:
    return SimpleNamespace(
        state=SimpleNamespace(
            inbox_service=None,
            team_stream=object(),
            messaging_service=None,
            messaging_registry=None,
            messaging_backend=None,
        )
    )


@pytest.mark.asyncio
async def test_failed_start_rebuilds_and_publishes_complete_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    backend = _Backend()
    service = object()
    calls = 0

    async def _build() -> tuple[Any, Any, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            return None, None, None
        return service, object(), backend

    class _Observer:
        def __init__(self, *_args: Any) -> None:
            pass

        async def run(self, *, interval: float) -> None:
            await asyncio.Event().wait()

    monkeypatch.setattr(lifecycle, "build_messaging_service", _build)
    monkeypatch.setattr(lifecycle, "build_team_post_forwarder", lambda **_kwargs: object())
    monkeypatch.setattr(lifecycle, "TeamBusObserver", _Observer)
    owner = lifecycle.MessagingLifecycle(
        app,
        required=True,
        injected_service=None,
        injected_forwarder=None,
        outbox=object(),
        observer_interval=1.0,
    )
    await owner.start()
    assert app.state.messaging_service is None
    try:
        for _ in range(20):
            if app.state.messaging_service is service:
                break
            await asyncio.sleep(0.05)
        assert app.state.messaging_service is service
        assert app.state.messaging_backend is backend
        assert app.state.team_post_forwarder is not None
    finally:
        await owner.aclose()
    assert backend.closed


@pytest.mark.asyncio
async def test_failed_setup_closes_backend_without_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = _app()
    backend = _Backend()

    async def _build() -> tuple[Any, Any, Any]:
        return object(), object(), backend

    def _reject(**_kwargs: Any) -> None:
        raise RuntimeError("forwarder unavailable")

    monkeypatch.setattr(lifecycle, "build_messaging_service", _build)
    monkeypatch.setattr(lifecycle, "build_team_post_forwarder", _reject)
    owner = lifecycle.MessagingLifecycle(
        app,
        required=True,
        injected_service=None,
        injected_forwarder=None,
        outbox=object(),
        observer_interval=1.0,
    )
    await owner.start()
    assert app.state.messaging_service is None
    assert backend.closed
    await owner.aclose()


@pytest.mark.asyncio
async def test_standalone_lifecycle_never_imports_optional_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import builtins

    real_import = builtins.__import__

    def _deny_arcteam(name: str, *args: Any, **kwargs: Any) -> Any:
        if name.startswith("arcteam"):
            raise ImportError("optional fleet removed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _deny_arcteam)
    owner = lifecycle.MessagingLifecycle(
        _app(),
        required=False,
        injected_service=None,
        injected_forwarder=None,
        outbox=object(),
        observer_interval=1.0,
    )
    await owner.start()
    await owner.aclose()
