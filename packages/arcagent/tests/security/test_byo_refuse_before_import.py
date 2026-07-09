"""SPEC-047 AC-2 — BYO refuse-before-import through the REAL module runtime path.

The producers-unwired defense demands this be proven through the actual production
seam (``modules/*/_runtime.configure``), not a direct ``select_extension`` call. A
non-allowlisted dotted BYO brain/adapter selected above the personal tier must make
startup fail closed AND never import the BYO module — asserted with a filesystem
import-side-effect sentinel: the sentinel module writes a marker file on import, so if
the marker never appears the module was genuinely never imported (ASI04).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from arcagent.modules.memory import _runtime as memory_runtime
from arcagent.modules.skills import _runtime as skills_runtime


def _install_sentinel(tmp_path: Path, module_name: str, class_name: str) -> Path:
    """Drop a BYO module on ``sys.path`` that touches a marker file when imported."""
    marker = tmp_path / f"{module_name}.imported"
    module_src = tmp_path / f"{module_name}.py"
    module_src.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker)!r}).write_text('imported')\n"
        f"class {class_name}:\n"
        "    def __init__(self, *a, **k):\n"
        "        pass\n",
        encoding="utf-8",
    )
    sys.path.insert(0, str(tmp_path))
    return marker


def _cleanup(tmp_path: Path, module_name: str) -> None:
    sys.path[:] = [p for p in sys.path if p != str(tmp_path)]
    sys.modules.pop(module_name, None)


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_byo_brain_refused_before_import_via_configure(tmp_path: Path, tier: str) -> None:
    marker = _install_sentinel(tmp_path, "sentinel_byo_brain", "EvilBrain")
    try:
        with pytest.raises(ValueError, match="allowlist"):
            memory_runtime.configure(
                config={
                    "brain": "sentinel_byo_brain:EvilBrain",
                    "tier": tier,
                    "brain_allowlist": [],
                },
                workspace=tmp_path,
                agent_did="did:arc:agent",
            )
        assert not marker.exists(), "BYO brain module was imported — the gate leaked (ASI04)"
    finally:
        _cleanup(tmp_path, "sentinel_byo_brain")


@pytest.mark.parametrize("tier", ["enterprise", "federal"])
def test_byo_adapter_refused_before_import_via_configure(tmp_path: Path, tier: str) -> None:
    marker = _install_sentinel(tmp_path, "sentinel_byo_adapter", "EvilAdapter")
    try:
        with pytest.raises(ValueError, match="allowlist"):
            skills_runtime.configure(
                config={
                    "adapter": "sentinel_byo_adapter:EvilAdapter",
                    "tier": tier,
                    "adapter_allowlist": [],
                },
                workspace=tmp_path,
                agent_did="did:arc:agent",
            )
        assert not marker.exists(), "BYO adapter module was imported — the gate leaked (ASI04)"
    finally:
        _cleanup(tmp_path, "sentinel_byo_adapter")
