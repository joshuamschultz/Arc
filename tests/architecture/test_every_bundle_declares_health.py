"""Every connector bundle declares how its connection is health-checked (P18-1).

A bundle with no ``[health]`` block is a card that says "Not checked yet" forever:
the probe loop has nothing to run. The check is parametrised off the filesystem,
so a bundle added tomorrow (including one an operator generates) is covered
without touching this file.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from arcagent.core.tier import Tier
from arcagent.extension.manifest import load_manifest

EXTENSIONS = Path(__file__).resolve().parents[2] / "extensions"
BUNDLES = sorted(path.parent for path in EXTENSIONS.glob("*/extension.toml"))


def test_the_extension_tree_is_found() -> None:
    assert BUNDLES, f"no extension bundles under {EXTENSIONS}"


@pytest.mark.parametrize("bundle", BUNDLES, ids=lambda path: path.name)
def test_every_bundle_declares_health(bundle: Path) -> None:
    manifest = load_manifest((bundle / "extension.toml").read_text(), tier=Tier.PERSONAL)

    assert manifest.health is not None, f"{bundle.name} has no [health] block"
    if manifest.health.mode == "none":
        assert manifest.health.reason
