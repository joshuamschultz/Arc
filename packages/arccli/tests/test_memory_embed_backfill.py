"""Tests for ``arc memory embed-backfill`` — watch or finish the doc-pool vector backfill.

Connected-document chunks written while the embedder could not serve are stored
without a vector; the serving process backfills them in the background. This
command is the operator's view of that work (read-only, safe while ``arc ui``
runs) and, with ``--run`` while the service is stopped, a one-shot run to
completion. ``--run`` refuses a store another process owns, because two
processes writing one ``index.db`` make the service rebuild its vector sidecar
from scratch again and again.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
from pathlib import Path
from typing import Any

import pytest
from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB, sqlite_vec_loadable
from arcmemory.doc_index import DocIndex, doc_scope
from arcmemory.index import ann
from arcmemory.index.backfill import read_embed_backlog
from arcmemory.index.source import SourceChunk

from arccli.commands import memory as memory_cmd
from arccli.commands.memory import memory_handler

pytestmark = pytest.mark.skipif(
    not sqlite_vec_loadable(), reason="sqlite-vec extension not loadable here"
)

_DID = "did:arc:local:executor/olivia01"
_PRINCIPAL = "did:arc:knowledge:0123456789abcdef0123456789abcdef"


class _Wide:
    """Deterministic 384-dim vectors (the default index width)."""

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([digest[i % 32] / 255.0 for i in range(384)])
        return out


@pytest.fixture(autouse=True)
def _forget_indexes() -> Any:
    yield
    ann.forget_loaded_indexes()


def _seed(workspace: Path, owner: str, count: int) -> str:
    return asyncio.run(_seed_async(workspace, owner, count))


async def _seed_async(workspace: Path, owner: str, count: int) -> str:
    db = MemoryDB(workspace)
    try:
        await DocIndex(db, workspace, MemoryConfig(), embedder=None).index_source(
            "dropbox",
            owner,
            [
                SourceChunk(
                    chunk_id=f"dropbox:o{i}#0",
                    source_path=f"connected/dropbox/{i}.md",
                    text=f"dropbox doc {i}",
                    classification="unclassified",
                    mtime=float(i),
                )
                for i in range(count)
            ],
        )
    finally:
        db.close()
    return doc_scope(owner, "dropbox").key


def _team(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A team dir with one agent that embeds locally; returns its workspace."""
    agent = tmp_path / "team" / "olivia"
    agent.mkdir(parents=True)
    (agent / "arcagent.toml").write_text(
        f'[agent]\nname = "olivia"\n\n[identity]\ndid = "{_DID}"\n\n'
        '[modules.memory.config]\ntier = "personal"\n\n'
        '[modules.memory.config.backend]\nembed_backend = "local"\n'
        'embed_model = "all-MiniLM-L6-v2"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(memory_cmd, "build_embedder", lambda *_a, **_k: _Wide())
    return agent / "workspace"


def _pending(workspace: Path) -> int:
    return sum(b.pending for b in read_embed_backlog(workspace / "memory" / "index.db").values())


def test_the_readout_lists_each_pool_and_writes_nothing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = tmp_path / "ws"
    scope = _seed(workspace, _DID, 7)

    memory_handler(["embed-backfill", str(workspace)])

    out = capsys.readouterr().out
    assert scope in out
    assert "0 of 7 embedded, 7 pending" in out
    assert "backfill owner: none" in out
    assert _pending(workspace) == 7


def test_run_needs_an_agent_for_its_embedder(tmp_path: Path) -> None:
    workspace = tmp_path / "ws"
    _seed(workspace, _DID, 1)
    with pytest.raises(SystemExit) as exc:
        memory_handler(["embed-backfill", "--run", str(workspace)])
    assert exc.value.code == 2


def test_run_embeds_the_agents_own_store_to_completion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _team(tmp_path, monkeypatch)
    _seed(workspace, _DID, 30)

    memory_handler(["embed-backfill", "--agent", "olivia", "--run"])

    assert _pending(workspace) == 0
    assert "30 vector(s) written" in capsys.readouterr().out


def test_run_refuses_a_store_the_service_owns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    workspace = _team(tmp_path, monkeypatch)
    _seed(workspace, _DID, 3)
    lock_path = workspace / "memory" / ".embed-backfill.lock"
    with lock_path.open("a+") as service:  # another process's claim
        fcntl.flock(service.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        service.write("777\n")
        service.flush()
        with pytest.raises(SystemExit) as exc:
            memory_handler(["embed-backfill", "--agent", "olivia", "--run"])
        fcntl.flock(service.fileno(), fcntl.LOCK_UN)
    assert exc.value.code == 1
    assert "pid 777" in capsys.readouterr().err
    assert _pending(workspace) == 3


def test_shared_stores_are_included_only_when_embedded_the_same_way(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from arcagent.modules.connected_data.ingest import profile_of

    workspace = _team(tmp_path, monkeypatch)
    _seed(workspace, _DID, 2)
    connected = tmp_path / "team" / "shared" / "connected"
    same = connected / "aaaa"
    other = connected / "bbbb"
    _seed(same, _PRINCIPAL, 4)
    _seed(other, _PRINCIPAL, 5)
    (same / ".embedding-profile").write_text(profile_of("local", "all-MiniLM-L6-v2", ""))
    (other / ".embedding-profile").write_text("some-other-model")

    memory_handler(["embed-backfill", "--agent", "olivia", "--shared", "--run"])

    assert _pending(workspace) == 0
    assert _pending(same) == 0
    assert _pending(other) == 5
    assert "embedded another way" in capsys.readouterr().err
