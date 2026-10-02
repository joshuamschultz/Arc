"""Shared fixtures for the arcmemory suite."""

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path

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
