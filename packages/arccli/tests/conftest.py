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
from unittest.mock import patch

import pytest


@pytest.fixture(autouse=True, scope="session")
def _block_browser_launch() -> Iterator[None]:
    """Prevent every test from spawning a real browser window.

    Patches the canonical `webbrowser.open` symbol once at session start.
    Returns True so callers that branch on success keep their happy path,
    but no actual browser is ever invoked.
    """
    with patch("webbrowser.open", return_value=True):
        yield
