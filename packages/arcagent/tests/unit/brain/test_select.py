"""Brain selection — the generic, backend-agnostic seam (SPEC-041 / SPEC-047).

arcagent names no memory backend. ``select_brain`` resolves ``[modules.memory] brain``
generically:

* a bare backend name → that package's ``build_brain(context)`` entrypoint (ungated,
  degrade-to-NullBrain-with-a-warning if the package is absent);
* a ``module:Class`` path → BYO, which must NEVER be imported above the personal tier
  unless operator-allowlisted (ASI04 / the Sign pillar) — importing an unverified
  class-path at startup is arbitrary code execution.

The arcmemory-specific *construction* wiring (embedder / distiller / loop-model /
dynamics) lives in ``arcmemory.build_brain`` and is tested in arcmemory, not here.
"""

from __future__ import annotations

import importlib
import logging
import types
from pathlib import Path

import pytest

from arcagent.brain import NullBrain, select, select_brain


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

    # SPEC-047: the dotted-path import lives in the shared select_extension mechanism.
    monkeypatch.setattr(importlib, "import_module", fake_import)
    return calls


_PATH = "byo_brain_mod:_FakeBrain"


# -- generic provider path (arcmemory resolves through it, un-named in source) ----------


def test_named_backend_resolves_via_build_brain(tmp_path: Path) -> None:
    """``brain="arcmemory"`` is the generic provider path — arcagent imports the named
    package and calls its ``build_brain``; arcmemory is installed here, so a real Brain
    (never a NullBrain) comes back, with no arcmemory symbol named in arcagent source."""
    import arcmemory

    brain = select_brain(
        "arcmemory",
        workspace=tmp_path,
        agent_did="did:arc:a",
        tier="personal",
        backend_config={"embed_backend": "none"},
    )
    assert isinstance(brain, arcmemory.ArcMemoryBrain)


def test_uninstalled_backend_degrades_to_nullbrain(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A named backend that is not installed degrades to NullBrain WITH a warning — the
    agent still boots (REQ-005)."""
    with caplog.at_level(logging.WARNING, logger="arcagent.brain.select"):
        brain = select_brain(
            "no_such_memory_pkg",
            workspace=tmp_path,
            agent_did="did:arc:a",
            tier="personal",
        )
    assert isinstance(brain, NullBrain)
    assert caplog.records, "an uninstalled backend must warn as it degrades"


def test_none_selects_nullbrain(tmp_path: Path) -> None:
    brain = select_brain("none", workspace=tmp_path, agent_did="did:arc:a")
    assert isinstance(brain, NullBrain)


# -- BYO class-path sign/allowlist gate (unchanged security invariant) ------------------


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
