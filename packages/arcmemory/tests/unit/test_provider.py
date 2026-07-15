"""``build_brain`` — the well-known provider entrypoint arcagent's generic seam calls.

arcagent names no arcmemory symbol: it lazily imports the configured backend module
and calls its ``build_brain(context)``. This is arcmemory's side of that contract — it
owns constructing its own ``ArcMemoryBrain`` (embedder + distiller + loop-model seams
wired to arcllm) from the generic context dict arcagent passes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import arcllm
import pytest

import arcmemory
from arcmemory import build_brain


def _context(tmp_path: Path, **backend: object) -> dict[str, Any]:
    return {
        "workspace": tmp_path,
        "agent_did": "did:arc:a",
        "tier": "personal",
        "audit_sink": None,
        "identity": None,
        "policy_pipeline": None,
        "backend_config": dict(backend),
    }


def test_build_brain_returns_arcmemory_brain(tmp_path: Path) -> None:
    brain = build_brain(_context(tmp_path, embed_backend="none"))
    assert isinstance(brain, arcmemory.ArcMemoryBrain)


def test_build_brain_threads_model_identity_and_pipeline(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Guards the producers-unwired trap: agentic consolidation is DEAD (model=None)
    and writes UNSIGNED unless build_brain threads model + identity + policy_pipeline.
    """
    recorded: dict[str, object] = {}

    class _SpyBrain:
        def __init__(self, _workspace: Path, _agent_did: str, **kw: object) -> None:
            recorded.update(kw)

    monkeypatch.setattr("arcmemory.provider.ArcMemoryBrain", _SpyBrain)
    monkeypatch.setattr(arcllm, "load_model", lambda *_a, **_k: "MODEL")
    ident, pipe = object(), object()

    ctx = _context(tmp_path, embed_backend="none", distill_provider="anthropic", distill_model="claude")
    ctx["identity"] = ident
    ctx["policy_pipeline"] = pipe
    build_brain(ctx)

    assert recorded["model"] == "MODEL"
    assert recorded["identity"] is ident
    assert recorded["policy_pipeline"] is pipe


def test_build_brain_dynamics_override_reaches_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``backend_config['dynamics']`` overrides must reach arcmemory's MemoryConfig."""
    recorded: dict[str, object] = {}

    class _SpyBrain:
        def __init__(self, _workspace: Path, _agent_did: str, *, config: object = None, **_k: object) -> None:
            recorded["config"] = config

    monkeypatch.setattr("arcmemory.provider.ArcMemoryBrain", _SpyBrain)

    build_brain(
        _context(tmp_path, embed_backend="none", dynamics={"entity_merge_candidate_threshold": 0.55})
    )
    cfg = recorded["config"]
    assert cfg.entity_merge_candidate_threshold == 0.55  # type: ignore[attr-defined]


def test_build_brain_distiller_rides_budget_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    """The distiller's per-run provider must be loaded WITH telemetry (budget-wrapped)."""
    from arcmemory.provider import build_distiller

    captured: dict[str, object] = {}

    class _Ctx:
        async def __aenter__(self) -> object:
            return object()

        async def __aexit__(self, *exc: object) -> None:
            return None

    def fake_load_model(provider: str, model: object = None, **kwargs: object) -> _Ctx:
        captured["provider"] = provider
        captured["telemetry"] = kwargs.get("telemetry")
        return _Ctx()

    monkeypatch.setattr(arcllm, "load_model", fake_load_model)

    distiller = build_distiller("anthropic", "", "did:arc:agent")
    assert distiller is not None
    distiller._provider_factory()

    telemetry = captured["telemetry"]
    assert isinstance(telemetry, dict) and telemetry.get("agent_did") == "did:arc:agent"
