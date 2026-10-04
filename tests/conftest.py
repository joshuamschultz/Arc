"""Top-level test configuration for architecture tests.

Ensures workspace sibling packages (arcllm, etc.) are on sys.path when running
architecture tests from the workspace root via `uv run pytest tests/`.

The architecture test for ExecutorBackend (test_backend_protocol_duck_typing.py)
imports from arcrun.backends, which triggers arcrun.__init__ → arcllm import.
arcllm is a workspace-editable package whose source is in packages/arcllm/src.
"""

from __future__ import annotations

import inspect
import sys
from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest


def _ensure_workspace_packages_on_path() -> None:
    """Add workspace package src directories to sys.path if not already present."""
    workspace_root = Path(__file__).parents[1]  # Arc/
    packages_dir = workspace_root / "packages"
    if not packages_dir.exists():
        return
    for pkg_src in packages_dir.glob("*/src"):
        if pkg_src.is_dir() and str(pkg_src) not in sys.path:
            sys.path.insert(0, str(pkg_src))


_ensure_workspace_packages_on_path()


@pytest.fixture
async def _stop_sync_worker_in_loop() -> AsyncIterator[None]:
    yield
    from arcagent.modules.connected_data.sync_worker.supervisor import (
        shutdown_process_supervisor,
    )

    await shutdown_process_supervisor()


@pytest.fixture(autouse=True)
def _no_sync_worker_outlives_its_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """A test that wrote a connected store started a sync worker; it dies with the test.

    A coroutine test stops it in order on its own loop; anything left over (a
    supervisor started on a loop that is gone) is killed outright.
    """
    if inspect.iscoroutinefunction(getattr(request, "function", None)):
        request.getfixturevalue("_stop_sync_worker_in_loop")
    yield
    from arcagent.modules.connected_data.sync_worker.supervisor import (
        discard_process_supervisor,
    )

    discard_process_supervisor()
