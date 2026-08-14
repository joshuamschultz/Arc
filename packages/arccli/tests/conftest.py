"""Session-wide test fixtures for the arccli test suite.

The session-autouse `_block_browser_launch` fixture replaces
`webbrowser.open` with a no-op return-True stub for the entire test
session. Without this, any test that exercises the `arc ui start` flow
(directly or via TestClient lifespan events) would launch a real browser
window — once per test, leaving stacks of tabs open between runs.

Tests that explicitly want to assert on `webbrowser.open` arguments can
still patch it locally; the local patch shadows the session stub for the
duration of the test.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from arctrust.paths import ARC_CONFIG_DIR_ENV


@pytest.fixture
def team_backend(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Inject a shared in-memory arcteam backend for in-process CLI tests.

    ``arc team``/``arc agent create`` build their arcteam service through
    ``arccli.commands.team._connect_backend`` (NATS on the live path). Tests
    replace that factory with a single :class:`~arcteam.storage.MemoryBackend`
    instance so every ``_build_service`` call in the test shares one isolated,
    server-free store. Returns the backend so assertions can read it back.
    """
    from arcteam.storage import MemoryBackend

    backend = MemoryBackend()

    async def _fake_connect() -> Any:
        return backend

    monkeypatch.setattr("arccli.commands.team._connect_backend", _fake_connect)
    return backend


@pytest.fixture(autouse=True, scope="session")
def _block_browser_launch() -> Iterator[None]:
    """Prevent every test from spawning a real browser window.

    Patches the canonical `webbrowser.open` symbol once at session start.
    Returns True so callers that branch on success keep their happy path,
    but no actual browser is ever invoked.
    """
    with patch("webbrowser.open", return_value=True):
        yield


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
