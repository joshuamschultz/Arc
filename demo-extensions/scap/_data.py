"""Lazy-loaded bundled reference data (NIST OSCAL, FedRAMP baselines, CTID ATT&CK).

Loaded once per process and cached. Files live under
``~/.arc/capabilities/scap/data/`` (D-372).
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

_DATA_DIR = Path(__file__).resolve().parent / "data"


@lru_cache(maxsize=1)
def nist_catalog() -> dict:
    """NIST 800-53 Rev 5 catalog subset."""
    with (_DATA_DIR / "nist_800_53_rev5.json").open() as f:
        return json.load(f)


@lru_cache(maxsize=1)
def fedramp_baselines() -> dict[str, list[str]]:
    """Map of baseline name → list of control IDs."""
    raw = json.loads((_DATA_DIR / "fedramp_baselines.json").read_text())
    return {k: v for k, v in raw.items() if k in ("low", "moderate", "high")}


@lru_cache(maxsize=1)
def attack_map() -> dict:
    """CTID 800-53 → ATT&CK technique mapping (curated subset)."""
    with (_DATA_DIR / "attack_to_800_53.json").open() as f:
        return json.load(f)


def control_title(control_id: str) -> str:
    """Look up a control's human-readable title (or empty string)."""
    cat = nist_catalog()
    return cat.get("controls", {}).get(control_id, {}).get("title", "")


def control_family(control_id: str) -> str:
    """Return the family prefix (AC, AU, CM, ...). Falls back to substring."""
    cat = nist_catalog()
    fam = cat.get("controls", {}).get(control_id, {}).get("family", "")
    if fam:
        return fam
    return control_id.split("-")[0] if "-" in control_id else ""


def in_baseline(control_id: str, baseline: str) -> bool:
    """True if control_id is part of the named FedRAMP baseline."""
    return control_id in fedramp_baselines().get(baseline, [])


def techniques_for(control_id: str) -> list[dict]:
    """Return list of ATT&CK technique dicts mapped to this control."""
    return attack_map().get("controls", {}).get(control_id, [])


def threat_narrative_for(control_id: str) -> str:
    """Plain-language threat framing for a control. Falls back to empty."""
    return attack_map().get("narratives", {}).get(control_id, "")
