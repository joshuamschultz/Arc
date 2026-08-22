"""Shared arcagent test fixtures."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import freezegun
import pytest
from arcstore.backends import ArcStoreBackend
from arcstore.backends.memory import FakeBackend

# freezegun patches datetime references by walking every module in sys.modules.
# Once a memory test has imported the arcmemory brain, sentence-transformers has
# pulled `transformers` in, and walking it triggers that package's lazy module
# machinery — which raises on an unrelated broken submodule. The failure then
# lands on whichever test next calls @freeze_time, so the scheduler suite fails
# only when a memory test ran first.
#
# Nothing under `transformers` holds a datetime freezegun needs to patch, so
# skipping it is the correct fix rather than a workaround.
freezegun.configure(extend_ignore_list=["transformers"])


@pytest.fixture
async def arcstore_opener() -> AsyncIterator[Callable[[], Awaitable[ArcStoreBackend]]]:
    """Return one started in-memory ArcStore backend for a test runtime."""
    backend = FakeBackend()
    await backend.start()

    async def open_fake_backend() -> ArcStoreBackend:
        return backend

    try:
        yield open_fake_backend
    finally:
        await backend.stop()


@pytest.fixture(autouse=True)
def _isolate_arcstore_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the shared arcstore data dir at a per-test tmp dir.

    The policy WORM chain now defaults into ``<data_dir>/worm/`` (so the arcui
    Security-screen ingest tails it). Without isolation, every full-startup test
    would open a ``WormSink`` on the real ``~/.arc/store/worm/audit-chain-<ws>.jsonl``
    and collide across tests on the single-writer flock. Env wins in
    ``resolve_data_dir`` precedence, so any test that also reads/writes the store
    stays self-consistent under this same dir.
    """
    monkeypatch.setenv("ARCSTORE_DATA_DIR", str(tmp_path / "arcstore"))
    # ARCSTORE_DATA_DIR alone is not enough. Anything resolving through
    # arctrust.paths — the operator key, identity, trust store, module root —
    # still lands in the developer's REAL ~/.arc without this, which is how four
    # stray audit chains ended up in a live deployment. A suite whose result
    # depends on what else is running on the machine is diagnosing the machine.
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))
