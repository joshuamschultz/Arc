"""SPEC-073 Phase E — arcui's knowledge routes must wire a real embedder.

``_operator_for`` (knowledge.py) currently builds ``MemoryOperator`` with no
``embedder=``, so every arcui-driven ``document_search`` degrades to BM25+graph
and ``index_health`` under-reports even when the agent's own
``[modules.memory]`` config asks for a live embedder. Phase E makes
``_operator_for`` read that config and pass ``arcmemory.provider.build_embedder``'s
result through.

RED reason: ``_operator_for`` never calls ``build_embedder`` at all today, so
the spy records zero calls and ``op._embedder`` is always ``None`` regardless
of what the agent's ``arcagent.toml`` asks for — the first test's ``assert
calls`` fails.

The spy is installed at BOTH plausible import sites
(``arcui.routes.knowledge.build_embedder`` and
``arcmemory.provider.build_embedder``) so the test binds to whichever import
style the real implementation ends up using, per SPEC-073's contract.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import arcmemory.provider as provider_mod
import arcui.routes.knowledge as knowledge_mod
from arcui.routes.knowledge import _operator_for


class _SentinelEmbedder:
    """A distinguishable placeholder — never a real embedder implementation."""


def _write_agent_toml(
    agent_root: Path, *, embed_backend: str | None, embed_model: str = "m"
) -> None:
    agent_root.mkdir(parents=True, exist_ok=True)
    lines = [
        "[agent]",
        'name = "alpha"',
        'org = "research"',
        "[identity]",
        'did = "did:arc:alpha"',
    ]
    if embed_backend is not None:
        lines += [
            "[modules.memory]",
            f'embed_backend = "{embed_backend}"',
            f'embed_model = "{embed_model}"',
        ]
    (agent_root / "arcagent.toml").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _patch_build_embedder(monkeypatch: pytest.MonkeyPatch, spy: Any) -> None:
    monkeypatch.setattr(provider_mod, "build_embedder", spy)
    monkeypatch.setattr(knowledge_mod, "build_embedder", spy, raising=False)


def test_operator_for_wires_the_configured_embedder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sentinel = _SentinelEmbedder()
    calls: list[tuple[Any, ...]] = []

    def _spy(agent_did: str, backend: str, model: str, *, base_url: str = "") -> Any:
        calls.append((agent_did, backend, model, base_url))
        return sentinel

    _patch_build_embedder(monkeypatch, _spy)

    agent_root = tmp_path / "alpha_agent"
    _write_agent_toml(agent_root, embed_backend="local", embed_model="m")

    op = _operator_for(agent_root, "did:arc:alpha")

    assert calls, "build_embedder was never called — _operator_for does not wire an embedder"
    did, backend, model, _base_url = calls[0]
    assert (did, backend, model) == ("did:arc:alpha", "local", "m")
    assert op._embedder is sentinel


def test_operator_for_has_no_embedder_when_backend_is_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _spy(*_args: Any, **_kwargs: Any) -> Any:
        return _SentinelEmbedder()

    _patch_build_embedder(monkeypatch, _spy)

    agent_root = tmp_path / "beta_agent"
    _write_agent_toml(agent_root, embed_backend="none")

    op = _operator_for(agent_root, "did:arc:beta")

    assert op._embedder is None
