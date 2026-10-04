"""Only a provider-rebuildable store trades the per-commit fsync for loop time.

The connection-scoped shared store (P18-4) is re-derivable from the provider,
so its index commits skip the fsync (``synchronous=NORMAL``). An agent's own
workspace store also holds its episodic raw stream and keeps ``FULL``. Both are
written only by the sync worker, so that is where the setting must hold.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust.paths import connected_knowledge_dir

from arcagent.extension.knowledge_subscriptions import store_key
from arcagent.modules.connected_data.sync_worker.specs import StoreSpec
from arcagent.modules.connected_data.sync_worker.worker import StoreWriters

_DID = "did:arc:test:agent-a"


class _NoHost:
    async def call(self, op: str, args: dict[str, Any]) -> Any:
        raise AssertionError(f"opening a store asked the host for {op}")


def _synchronous(writers: StoreWriters, spec: StoreSpec) -> int:
    adapter = writers._open(spec).adapter
    conn = adapter._connected_service()._db.connect()
    return int(conn.execute("PRAGMA synchronous").fetchone()[0])


@pytest.mark.asyncio
async def test_shared_connection_store_commits_without_fsync(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc"))
    writers = StoreWriters(_NoHost())  # type: ignore[arg-type]  # reason: opening needs no host
    spec = StoreSpec(
        kind="shared",
        agent_did=_DID,
        root=str(connected_knowledge_dir() / store_key("wiki")),
        authority="subscriber",
        connection_id="wiki",
        approval_id="approval-1",
    )
    try:
        assert _synchronous(writers, spec) == 1  # NORMAL
    finally:
        await writers.close()


@pytest.mark.asyncio
async def test_agent_own_connected_store_keeps_full_durability(tmp_path: Path) -> None:
    agent_dir = tmp_path / "agent"
    (agent_dir / "workspace").mkdir(parents=True)
    (agent_dir / "arcagent.toml").write_text(
        f'[agent]\nworkspace = "./workspace"\n[identity]\ndid = "{_DID}"\n', encoding="utf-8"
    )
    writers = StoreWriters(_NoHost())  # type: ignore[arg-type]  # reason: opening needs no host
    spec = StoreSpec(
        kind="own",
        agent_did=_DID,
        root=str((agent_dir / "workspace").resolve()),
        authority="owner",
        config_path=str(agent_dir / "arcagent.toml"),
    )
    try:
        assert _synchronous(writers, spec) == 2  # FULL
    finally:
        await writers.close()
