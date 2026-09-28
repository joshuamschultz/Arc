"""COMP-030: the layer-clean ``PromptSource`` seam.

Two layers, one order — agent override first, then system (stock) — behind a
single ``resolve(package, name) -> str`` contract that a package below the
agent (arcrun, arcmemory, arcskill) can depend on without knowing an agent
folder exists. ``StockPromptSource`` is the system-only default;
``ResolverPromptSource`` adapts an agent's overlay-aware ``PromptResolver`` or
frozen ``PromptSnapshot`` to the same contract.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from packages.arcprompt.tests.conftest import DirCatalog, SigningKey, write_overlay, write_stock

from arcprompt.errors import PromptMissing
from arcprompt.resolver import PromptResolver
from arcprompt.snapshot import PromptSnapshot
from arcprompt.source import PromptSource, ResolverPromptSource, StockPromptSource
from arcprompt.verifier import TrustPosture


def _resolver(overlay_root: Path, stock_root: Path, signer: SigningKey) -> PromptResolver:
    return PromptResolver(
        overlay_root=overlay_root,
        trusted_public_key=signer.public_key,
        posture=TrustPosture.FEDERAL,
        catalog=DirCatalog(stock_root),
    )


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


def test_stock_prompt_source_satisfies_prompt_source_at_runtime() -> None:
    assert isinstance(StockPromptSource(), PromptSource)


def test_resolver_prompt_source_satisfies_prompt_source_at_runtime(
    tmp_path: Path, signer: SigningKey
) -> None:
    resolver = _resolver(tmp_path / "overlays", tmp_path / "stock", signer)
    assert isinstance(ResolverPromptSource(resolver), PromptSource)


# ---------------------------------------------------------------------------
# StockPromptSource — system prompts only
# ---------------------------------------------------------------------------


def test_stock_prompt_source_returns_the_real_packaged_stock_body() -> None:
    import arcprompt

    body = StockPromptSource().resolve("arcagent", "base_system")
    assert body == arcprompt.load_stock("arcagent", "base_system")


def test_stock_prompt_source_raises_prompt_missing_for_unknown_prompt() -> None:
    with pytest.raises(PromptMissing) as exc:
        StockPromptSource().resolve("arcagent", "nonexistent-prompt-xyz")
    assert exc.value.package == "arcagent"
    assert exc.value.name == "nonexistent-prompt-xyz"


# ---------------------------------------------------------------------------
# ResolverPromptSource — override-then-fallback, via PromptResolver
# ---------------------------------------------------------------------------


def test_resolver_prompt_source_falls_back_to_stock_when_no_override(
    tmp_path: Path, signer: SigningKey
) -> None:
    overlay_root, stock_root = tmp_path / "overlays", tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    source = ResolverPromptSource(_resolver(overlay_root, stock_root, signer))
    assert source.resolve("arcrun", "greeting") == "stock body"


def test_resolver_prompt_source_prefers_override_over_stock(
    tmp_path: Path, signer: SigningKey
) -> None:
    overlay_root, stock_root = tmp_path / "overlays", tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    write_overlay(overlay_root, "arcrun", "greeting", "override body", signer=signer)
    source = ResolverPromptSource(_resolver(overlay_root, stock_root, signer))
    assert source.resolve("arcrun", "greeting") == "override body"


def test_resolver_prompt_source_raises_prompt_missing_for_unknown_prompt(
    tmp_path: Path, signer: SigningKey
) -> None:
    overlay_root, stock_root = tmp_path / "overlays", tmp_path / "stock"
    source = ResolverPromptSource(_resolver(overlay_root, stock_root, signer))
    with pytest.raises(PromptMissing):
        source.resolve("arcrun", "nonexistent")


# ---------------------------------------------------------------------------
# ResolverPromptSource — override-then-fallback, via a frozen PromptSnapshot
# ---------------------------------------------------------------------------


def test_resolver_prompt_source_wraps_a_frozen_snapshot(
    tmp_path: Path, signer: SigningKey
) -> None:
    overlay_root, stock_root = tmp_path / "overlays", tmp_path / "stock"
    write_stock(stock_root, "arcrun", "greeting", "stock body")
    write_overlay(overlay_root, "arcrun", "greeting", "override body", signer=signer)
    resolver = _resolver(overlay_root, stock_root, signer)
    snap = PromptSnapshot(
        {
            ("arcrun", "greeting"): resolver.resolve("arcrun", "greeting"),
        }
    )
    source = ResolverPromptSource(snap)
    assert source.resolve("arcrun", "greeting") == "override body"


def test_resolver_prompt_source_snapshot_raises_prompt_missing_for_unfrozen_prompt(
    tmp_path: Path, signer: SigningKey
) -> None:
    # The PromptSource contract promises PromptMissing for an absent prompt —
    # the snapshot-backed source must honor it, not leak the dict's KeyError.
    snap = PromptSnapshot({})
    source = ResolverPromptSource(snap)
    with pytest.raises(PromptMissing) as exc:
        source.resolve("arcrun", "never-frozen")
    assert exc.value.package == "arcrun"
    assert exc.value.name == "never-frozen"
