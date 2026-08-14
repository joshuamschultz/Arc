"""The global capabilities root resolves through the one resolver, not a literal.

:func:`arctrust.paths.capabilities_dir` is the single source of truth for the
operator-curated capability root, and it honors ``ARC_CONFIG_DIR`` so a test or
an alternate deployment can relocate the whole tree. Every consumer that
hardcoded ``Path("~/.arc/capabilities")`` silently opted out of that: an isolated
deployment scanned — and a test suite WROTE INTO — the invoking user's real arc
home. These cases pin the resolver so that cannot come back.

The unset-env default is asserted explicitly because it is the common path: the
fix must relocate the root under ``ARC_CONFIG_DIR`` and change nothing at all
without it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arctrust.paths import capabilities_dir

from arcagent.capabilities.inventory import (
    collect_capability_inventory,
    global_capabilities_root,
)

_TOOL = (
    "from arcagent.tools._decorator import tool\n"
    "@tool(description='ok', version='1.0.0')\n"
    "async def relocated() -> str:\n"
    "    return 'ok'\n"
)


def test_unset_arc_config_dir_keeps_todays_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """No env var → the capability root under the real home, as before the fix."""
    monkeypatch.delenv("ARC_CONFIG_DIR", raising=False)
    assert global_capabilities_root() == capabilities_dir()
    assert global_capabilities_root().is_relative_to(Path.home() / ".arc")


def test_arc_config_dir_relocates_the_global_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "isolated"))
    assert global_capabilities_root() == capabilities_dir(tmp_path / "isolated")


def test_the_root_is_resolved_per_call_not_frozen_at_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module-level constant would freeze whatever the env said at import.

    Tests and services routinely set ``ARC_CONFIG_DIR`` after this module is
    first imported, which is exactly when a frozen value points at the real home.
    """
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "first"))
    first = global_capabilities_root()
    monkeypatch.setenv("ARC_CONFIG_DIR", str(tmp_path / "second"))
    assert global_capabilities_root() != first
    assert global_capabilities_root() == capabilities_dir(tmp_path / "second")


@pytest.mark.asyncio
async def test_inventory_scans_the_relocated_global_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end: the real inventory seam reads the relocated root.

    Asserting the resolver alone would not catch a consumer that kept its own
    literal, which is precisely how this bug survived — so the capability is
    planted under ``ARC_CONFIG_DIR`` and found through the seam a real scan uses.
    """
    arc_home = tmp_path / "isolated"
    root = capabilities_dir(arc_home)
    root.mkdir(parents=True)
    (root / "relocated.py").write_text(_TOOL, encoding="utf-8")
    monkeypatch.setenv("ARC_CONFIG_DIR", str(arc_home))
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()

    items = await collect_capability_inventory(agent_dir)

    found = [item for item in items if item.name == "relocated"]
    assert found, f"relocated tool not scanned; saw {[i.name for i in items]}"
    assert found[0].source_root == "global"
