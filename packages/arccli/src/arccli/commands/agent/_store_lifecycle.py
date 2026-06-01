"""Ambient arcstore spin-up for the agent lifecycle (SPEC-026 FR-6).

``arc agent serve`` / ``run`` / ``arc team`` make the operational store ambient:
the spool dir is *always* created (guaranteeing call-now-see-later), and when
``[arcstore].enabled`` with a configured backend, a ``StoreIngest`` background
task (backfill → tail) is managed for the life of the process and stopped
cleanly on shutdown.

Spin-up is **fail-open** (AC-6.3): if the backend cannot start, the spool still
records and the agent starts anyway — store failure never blocks the agent.
"""

from __future__ import annotations

import contextlib
import logging
import tomllib
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from arcstore import ArcStoreConfig

_logger = logging.getLogger("arccli.agent.store")


def load_arcstore_config(agent_dir: Path) -> ArcStoreConfig:
    """Build ``ArcStoreConfig`` from the agent's ``[arcstore]`` TOML block.

    Missing block → defaults (enabled, sqlite). A malformed block is logged and
    falls back to defaults — config trouble must never stop the agent serving.
    """
    toml_path = agent_dir / "arcagent.toml"
    if not toml_path.is_file():
        return ArcStoreConfig()
    try:
        with toml_path.open("rb") as fh:
            data: dict[str, Any] = tomllib.load(fh)
        block = data.get("arcstore", {})
        return ArcStoreConfig(**block)
    except Exception:  # reason: fail-open — bad config must not block serving
        _logger.warning(
            "invalid [arcstore] config in %s; using defaults", toml_path, exc_info=True
        )
        return ArcStoreConfig()


@contextlib.asynccontextmanager
async def managed_store_ingest(
    config: ArcStoreConfig,
    *,
    worm_public_key: bytes | None = None,
) -> AsyncIterator[Any]:
    """Spin arcstore up around an agent run; tear it down cleanly on exit.

    Yields the running ``StoreIngest`` (or ``None`` when disabled/degraded). The
    spool dir is always created first so the producer write path works regardless
    of whether ingest starts (SDD §12.2).
    """
    data_dir = config.resolve_data_dir()
    # ALWAYS create the spool dir — the call-now-see-later guarantee does not
    # depend on the ingester starting.
    (data_dir / "spool").mkdir(parents=True, exist_ok=True)

    if not config.enabled:
        yield None
        return

    ingest, backend = await _try_start_ingest(config, data_dir, worm_public_key)
    try:
        yield ingest
    finally:
        if ingest is not None:
            await ingest.stop()
        if backend is not None:
            await backend.stop()


async def _try_start_ingest(
    config: ArcStoreConfig,
    data_dir: Path,
    worm_public_key: bytes | None,
) -> tuple[Any, Any]:
    """Open the backend and start ingest; fail-open to ``(None, None)`` (AC-6.3)."""
    backend = None
    ingest = None
    try:
        from arcstore.backends import open_backend
        from arcstore.ingest import StoreIngest

        (data_dir / "store").mkdir(parents=True, exist_ok=True)
        (data_dir / "worm").mkdir(parents=True, exist_ok=True)
        backend = open_backend(config.backend, data_dir / "store" / "arcstore.db")
        await backend.start()
        ingest = StoreIngest(
            backend,
            spool_dir=data_dir / "spool",
            worm_dir=data_dir / "worm",
            worm_public_key=worm_public_key,
        )
        await ingest.start()
        return ingest, backend
    except Exception:  # reason: fail-open — store failure must never block the agent (AC-6.3)
        _logger.warning(
            "arcstore ingest failed to start; agent continues with spool-only (degraded)",
            exc_info=True,
        )
        with contextlib.suppress(Exception):
            if ingest is not None:
                await ingest.stop()
        with contextlib.suppress(Exception):
            if backend is not None:
                await backend.stop()
        return None, None
