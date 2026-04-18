"""Unit test conftest.

Pre-imports slack_bolt into sys.modules so that the mock-based
slack adapter tests work correctly.

The test ``test_connect_raises_on_import_error`` uses:
    mods_to_remove = {k: v for k, v in sys.modules.items() if "slack_bolt" in k}
    with patch.dict(sys.modules, {k: None for k in mods_to_remove}):
        ...

This pattern only works when slack_bolt is ALREADY in sys.modules.
If slack_bolt is installed but not yet imported, mods_to_remove is empty,
patch.dict is a no-op, and connect() actually tries a real Slack connection.

Importing slack_bolt here (at conftest load time) ensures it's in sys.modules
before any test in this directory runs.
"""

from __future__ import annotations

import sys


def pytest_configure(config: object) -> None:
    """Pre-import slack_bolt so mock-based tests work correctly.

    This matches the pre-existing behavior of the test environment that
    had slack_bolt pre-loaded via the faker plugin or other imports.
    """
    try:
        import slack_bolt  # noqa: F401
    except ImportError:
        pass  # slack_bolt not installed — tests will be skipped or fail naturally
