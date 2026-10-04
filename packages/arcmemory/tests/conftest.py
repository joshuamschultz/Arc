"""Shared fixtures for the arcmemory suite."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

import numpy as np
import pytest
from arctrust.identity import AgentIdentity

from arcmemory.config import MemoryConfig
from arcmemory.db import MemoryDB
from arcmemory.okf_seal import bind_memory_identity, release_memory_identity
from arcmemory.types import Scope

_DIMS = 8


class StubEmbedder:
    """Deterministic, network-free embedder for rebuild/vec tests.

    Same text -> same vector, so a wipe+rebuild produces byte-identical vectors.
    Records ``calls`` so a test can assert the seam was (or was not) used.
    """

    def __init__(self, dims: int = _DIMS) -> None:
        self.dims = dims
        self.calls = 0

    async def embed_texts(self, texts: list[str]) -> list[list[float]]:
        self.calls += len(texts)
        out: list[list[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            out.append([digest[i] / 255.0 for i in range(self.dims)])
        return out


@pytest.fixture(autouse=True)
def _isolate_arc_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every ``arctrust.paths`` write inside this test's tmp dir.

    ``ArcMemoryBrain.register_datastore`` calls ``semantic_layer.overlay()``
    (H-025), which resolves its file through ``arctrust.paths.semantic_layer_file``
    -> ``operator_root()`` -> ``~/arc`` by default. Without this, any test that
    registers a datastore writes a real ``~/arc/config/semantic/<id>.toml`` on
    whatever machine runs the suite — the same class of leak the arcagent suite
    isolates via ``ARC_CONFIG_DIR``/``ARCSTORE_DATA_DIR``.
    """
    monkeypatch.setenv("ARC_TEAM_ROOT", str(tmp_path / "arc-team"))
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "arc-home"))


@pytest.fixture(autouse=True)
def _agent_signs_its_memory(tmp_path: Path) -> Iterator[None]:
    """Bind a fresh agent identity to this test's tmp dir (ADR-029: the agent signs
    its own memory indexes). Without a bound key every index read fails closed, as
    it does in production for a process that never built the agent's brain."""
    bind_memory_identity(tmp_path, AgentIdentity.generate(org="test", agent_type="memory"))
    yield
    release_memory_identity(tmp_path)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A fresh per-agent workspace directory."""
    return tmp_path / "agent-workspace"


@pytest.fixture
def db(workspace: Path) -> MemoryDB:
    """A per-agent MemoryDB opened at ``dims=8`` (small vectors for tests)."""
    memdb = MemoryDB(workspace, dims=_DIMS)
    memdb.connect()
    return memdb


@pytest.fixture
def scope() -> Scope:
    return Scope(agent_did="did:arc:test-agent")


@pytest.fixture
def config() -> MemoryConfig:
    return MemoryConfig()


@pytest.fixture
def embedder() -> StubEmbedder:
    return StubEmbedder()


def low_rank_vectors(n: int, dims: int, *, seed: int, rank: int = 32) -> np.ndarray:
    """Seeded unit vectors on a low-rank manifold, shaped like real sentence embeddings.

    Real embedding sets have a low intrinsic dimension; i.i.d. Gaussian points in
    384-d are a near-tie worst case no ANN index (or any real corpus) looks like.
    """
    rng = np.random.default_rng(seed)
    latent = rng.standard_normal((n, rank)).astype(np.float32)
    basis = rng.standard_normal((rank, dims)).astype(np.float32)
    raw = latent @ basis + 0.1 * rng.standard_normal((n, dims)).astype(np.float32)
    return (raw / np.linalg.norm(raw, axis=1, keepdims=True)).astype(np.float32)


def bulk_load_vectors(
    db: MemoryDB, scope: str, vectors: np.ndarray, *, prefix: str = "doc", start: int = 0
) -> list[str]:
    """Write chunk + fts + vec0 rows straight through SQL, the way an existing
    deployed index.db already holds them (no sidecar, no generation stamp).

    Returns the chunk ids in vector order. Text carries a shared ``corpus`` term
    plus a per-row token so BM25/graph channels have something to match.
    """
    conn = db.connect()
    ids = [f"{prefix}:{start + i}#0" for i in range(len(vectors))]
    for offset in range(0, len(ids), 5000):
        batch = ids[offset : offset + 5000]
        for i, chunk_id in enumerate(batch, start=offset):
            fts_rowid = conn.execute(
                "INSERT INTO fts_chunks (chunk_id, scope, text) VALUES (?, ?, ?)",
                (chunk_id, scope, f"corpus alpha row{start + i} topic{(start + i) % 97}"),
            ).lastrowid
            conn.execute(
                "INSERT INTO chunks (chunk_id, scope, source_path, mtime, classification, "
                "content_hash, embedded_hash, fts_rowid) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    chunk_id,
                    scope,
                    f"src/{chunk_id}",
                    float(start + i),
                    "unclassified",
                    f"h{start + i}",
                    f"h{start + i}",
                    fts_rowid,
                ),
            )
        conn.executemany(
            "INSERT INTO vec0 (chunk_id, embedding) VALUES (?, ?)",
            [(cid, vectors[i].tobytes()) for i, cid in enumerate(batch, start=offset)],
        )
        conn.commit()
    return ids
