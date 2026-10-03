"""J1 gate G12: connected documents are embedded with the embedder memory recalls with.

The memory module builds its embedder from ``[modules.memory.config]``; the ingest
port that fills the document index was built with none. Every synced page was
indexed lexically only, so a question that shares no words with its page found
nothing (1 of 10 right on the live path) while arcmemory's own gate, which
injects an embedder, stayed green.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arcstore.approvals import ApprovalStore
from arcstore.backends.memory import FakeBackend

from arcagent.modules.connected_data.ingest import ArcMemoryIngestAdapter
from arcagent.modules.memory import _runtime
from arcagent.modules.memory.config import MemoryConfig

_DID = "did:arc:embedder-agent"


@pytest.fixture(autouse=True)
def _reset() -> Any:
    _runtime.reset()
    yield
    _runtime.reset()


def _bind_memory(config: MemoryConfig) -> None:
    _runtime.bind(
        _runtime._State(
            config=config,
            brain=object(),
            workspace=Path("."),
            telemetry=None,
            bus=None,
            agent_did=_DID,
            active=True,
        )
    )


def _adapter(tmp_path: Path, **extra: Any) -> ArcMemoryIngestAdapter:
    return ArcMemoryIngestAdapter(
        tmp_path, _DID, approval_store=ApprovalStore(FakeBackend()), **extra
    )


def test_ingest_uses_the_embedder_the_memory_module_is_configured_with(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[tuple[str, str, str, str]] = []
    sentinel = object()

    def build_embedder(agent_did: str, backend: str, model: str, *, base_url: str = "") -> object:
        built.append((agent_did, backend, model, base_url))
        return sentinel

    monkeypatch.setattr("arcmemory.provider.build_embedder", build_embedder)
    _bind_memory(MemoryConfig(embed_backend="provider", embed_model="text-embed-x"))

    service = _adapter(tmp_path)._connected_service()

    assert service._embedder is sentinel
    assert built == [(_DID, "provider", "text-embed-x", "")]


def test_ingest_defaults_to_the_local_embedder_like_the_memory_brain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    built: list[str] = []

    def build_embedder(agent_did: str, backend: str, model: str, *, base_url: str = "") -> object:
        built.append(backend)
        return object()

    monkeypatch.setattr("arcmemory.provider.build_embedder", build_embedder)
    _bind_memory(MemoryConfig())

    _adapter(tmp_path)._connected_service()

    assert built == ["local"]


def test_ingest_without_the_memory_module_is_lexical_only(tmp_path: Path) -> None:
    assert _adapter(tmp_path)._connected_service()._embedder is None


def test_an_embedder_passed_in_wins(tmp_path: Path) -> None:
    chosen = object()
    _bind_memory(MemoryConfig())

    assert _adapter(tmp_path, embedder=chosen)._connected_service()._embedder is chosen
