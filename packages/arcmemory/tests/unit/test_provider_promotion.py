"""SPEC-083 COMP-023 — ``build_brain`` builds promotion from a plain config mapping.

arcagent hands over settings (never an arcllm object) and, when a fleet port is
attached, a publisher. arcmemory re-validates the mapping (federal lock included,
on the RAW tier label), constructs its own ``ArcllmPromotionClassifier``, and uses
the agent identity as the ledger signer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from arctrust.identity import AgentIdentity

from arcmemory import build_brain
from arcmemory.promotion.arcllm_classifier import ArcllmPromotionClassifier
from arcmemory.promotion.config import PromotionConfig, PromotionForbiddenAtTierError

_IDENTITY = AgentIdentity.generate(org="default", agent_type="executor")
_PUBLISHER = object()


def _context(tmp_path: Path, promotion: Any, *, tier: str = "personal") -> dict[str, Any]:
    return {
        "workspace": tmp_path,
        "agent_did": _IDENTITY.did,
        "tier": tier,
        "audit_sink": None,
        "identity": _IDENTITY,
        "policy_pipeline": None,
        "backend_config": {"embed_backend": "none"},
        "promotion_config": promotion,
        "promotion_publisher": _PUBLISHER,
    }


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    kwargs: dict[str, Any] = {}

    class _SpyBrain:
        def __init__(self, _workspace: Path, _agent_did: str, **kw: Any) -> None:
            kwargs.update(kw)

    monkeypatch.setattr("arcmemory.provider.ArcMemoryBrain", _SpyBrain)
    return kwargs


def test_enabled_mapping_builds_config_classifier_publisher_and_signer(
    tmp_path: Path, recorded: dict[str, Any]
) -> None:
    build_brain(_context(tmp_path, {"enabled": True, "classifier_model": "jev-1.13"}))

    assert recorded["promotion_config"] == PromotionConfig(enabled=True)
    classifier = recorded["promotion_classifier"]
    assert isinstance(classifier, ArcllmPromotionClassifier)
    assert classifier.classifier_id == "jev"
    assert recorded["promotion_publisher"] is _PUBLISHER
    assert recorded["promotion_signer"] is _IDENTITY


@pytest.mark.parametrize("promotion", [None, {}, {"enabled": False}])
def test_no_or_disabled_promotion_composes_nothing(
    tmp_path: Path, recorded: dict[str, Any], promotion: Any
) -> None:
    build_brain(_context(tmp_path, promotion))

    assert recorded["promotion_config"] is None
    assert recorded["promotion_classifier"] is None
    assert recorded["promotion_publisher"] is None
    assert recorded["promotion_signer"] is None


@pytest.mark.parametrize("tier", ["federal", " Federal "])
def test_enabled_at_federal_is_refused_even_with_odd_spelling(
    tmp_path: Path, recorded: dict[str, Any], tier: str
) -> None:
    with pytest.raises(PromotionForbiddenAtTierError):
        build_brain(_context(tmp_path, {"enabled": True}, tier=tier))


def test_invalid_settings_are_revalidated(tmp_path: Path, recorded: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="pinned"):
        build_brain(_context(tmp_path, {"enabled": True, "classifier_model": "jev-latest"}))
