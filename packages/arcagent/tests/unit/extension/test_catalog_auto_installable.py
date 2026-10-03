"""D18: the catalog says whether Arc can really install a bundle's host program.

A pin whose build names no ``member`` (an npm tarball) can never be placed, so the
button that offers to install it can never succeed. The catalog carries the answer
so a surface hides that button and tells the truth instead.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from arcagent.connection_catalog import catalog
from arcagent.core.tier import Tier

EXTENSIONS = Path(__file__).resolve().parents[5] / "extensions"


def _entry(name: str):  # type: ignore[no-untyped-def] # reason: test helper
    entries = {e.name: e for e in catalog(roots=[EXTENSIONS], tier=Tier.PERSONAL)}
    return entries[name]


def test_a_tarball_pin_with_no_binary_is_not_auto_installable() -> None:
    assert _entry("readwise_reader").auto_installable is False


def test_a_bundle_pinning_no_build_at_all_is_not_auto_installable() -> None:
    assert _entry("onepassword").auto_installable is False


def test_a_bundle_with_no_host_program_is_not_auto_installable() -> None:
    assert _entry("sqlite").auto_installable is False


def test_a_pinned_single_binary_for_this_host_is_auto_installable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("arcagent.connection_catalog.host_platform", lambda: "linux/amd64")
    assert _entry("github").auto_installable is True
