"""Canonical identity key for memory records — the UPSERT match key.

The distiller proposes slugs/ids as free LLM text, so the *same* real-world
entity arrives spelled differently across runs. ``canonical_slug`` folds those
variants to one form so distillation updates in place instead of minting a
duplicate file — and it strips path separators so an LLM key can never escape
the store directory.
"""

from __future__ import annotations

import pytest

from arcmemory.slug import canonical_slug


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Custom ERP", "custom-erp"),
        ("custom_erp", "custom-erp"),
        ("custom-erp", "custom-erp"),
        ("  Custom   ERP  ", "custom-erp"),
        ("coder_agent", "coder-agent"),
        ("coder-agent", "coder-agent"),
        ("browserbase-browse", "browserbase-browse"),
        ("bb_ui_test", "bb-ui-test"),
    ],
)
def test_variants_fold_to_one_canonical_form(raw: str, expected: str) -> None:
    assert canonical_slug(raw) == expected


def test_idempotent() -> None:
    once = canonical_slug("Custom ERP")
    assert canonical_slug(once) == once


def test_strips_path_separators_no_traversal() -> None:
    # An LLM-proposed key must never escape the store dir (LLM05).
    assert "/" not in canonical_slug("../../etc/passwd")
    assert ".." not in canonical_slug("../../etc/passwd")


def test_empty_or_punctuation_only_falls_back() -> None:
    assert canonical_slug("") == "unknown"
    assert canonical_slug("   ") == "unknown"
    assert canonical_slug("!!!") == "unknown"
