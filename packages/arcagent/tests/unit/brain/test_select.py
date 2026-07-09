"""Brain selection — BYO class-path sign/allowlist gate (F3) + distiller budget wiring.

``select_brain`` must never import an arbitrary config-supplied dotted class-path above
the personal tier (ASI04 / the Sign pillar): a BYO Brain is arbitrary code executed at
startup, so at enterprise/federal it is refused unless operator-allowlisted — fail-closed,
never imported. Personal may allow it (documented). Separately, the arcllm-backed distiller
must ride the SPEC-038 telemetry/budget just like the embedder, so a runaway consolidation
cannot make an unbounded distillation call (LLM10).
"""

from __future__ import annotations

import types
from pathlib import Path

import pytest

from arcagent.brain import select


class _FakeBrain:
    """A stand-in BYO Brain: accepts the ``(workspace, agent_did)`` construction contract."""

    def __init__(self, workspace: Path, agent_did: str) -> None:
        self.workspace = workspace
        self.agent_did = agent_did


def _patch_import(monkeypatch: pytest.MonkeyPatch) -> dict[str, int]:
    """Make ``module:_FakeBrain`` importable; record whether an import was attempted."""
    calls = {"imports": 0}
    mod = types.ModuleType("byo_brain_mod")
    mod._FakeBrain = _FakeBrain  # type: ignore[attr-defined]

    def fake_import(name: str) -> types.ModuleType:
        calls["imports"] += 1
        return mod

    monkeypatch.setattr(select.importlib, "import_module", fake_import)
    return calls


_PATH = "byo_brain_mod:_FakeBrain"


def test_byo_class_path_allowed_at_personal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_import(monkeypatch)
    brain = select.select_brain(_PATH, workspace=tmp_path, agent_did="did:arc:a", tier="personal")
    assert isinstance(brain, _FakeBrain)


def test_byo_class_path_refused_at_federal_without_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_import(monkeypatch)
    with pytest.raises(ValueError, match="allowlist"):
        select.select_brain(_PATH, workspace=tmp_path, agent_did="did:arc:a", tier="federal")
    assert calls["imports"] == 0, "must fail closed BEFORE importing an unverified class-path"


def test_byo_class_path_refused_at_enterprise_without_allowlist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _patch_import(monkeypatch)
    with pytest.raises(ValueError, match="allowlist"):
        select.select_brain(_PATH, workspace=tmp_path, agent_did="did:arc:a", tier="enterprise")
    assert calls["imports"] == 0


def test_byo_class_path_allowed_at_federal_when_allowlisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_import(monkeypatch)
    brain = select.select_brain(
        _PATH,
        workspace=tmp_path,
        agent_did="did:arc:a",
        tier="federal",
        brain_allowlist=(_PATH,),
    )
    assert isinstance(brain, _FakeBrain)  # allowlisted -> imported + instantiated


# -- distiller rides the SPEC-038 telemetry/budget --------------------------


def test_distiller_provider_is_built_with_budget_telemetry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The distiller's per-run provider must be loaded WITH telemetry (budget-wrapped)."""
    import arcllm
    import arcmemory

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

    distiller = select._build_distiller(arcmemory, "anthropic", "", "did:arc:agent")
    assert distiller is not None
    # Open the per-run provider the way consolidation would.
    distiller._provider_factory()  # type: ignore[attr-defined]

    telemetry = captured["telemetry"]
    assert isinstance(telemetry, dict) and telemetry.get("agent_did") == "did:arc:agent", (
        "consolidation LLM calls must ride the SPEC-038 telemetry/budget"
    )
