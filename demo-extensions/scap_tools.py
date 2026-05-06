"""SCAP extension shim — exposes the 6 @tool functions to arcagent's
flat capability scanner.

The framework's ``CapabilityLoader._scan_root`` walks
``~/.arc/capabilities/*.py`` (top level only — not subdirectories).
The actual extension lives in the ``scap/`` package next to this file
(``~/.arc/capabilities/scap/``); this shim imports the @tool-decorated
callables into module scope so the loader's ``vars(module).values()``
reflection finds them.

Per SPEC-024 D-372 / source doc §10 (dev-mode install).
"""

from __future__ import annotations

import sys
from pathlib import Path

# Make the sibling `scap` package importable without ~/.arc/capabilities
# needing to be on sys.path globally.
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from scap.ingest import scap_ingest, scap_query  # noqa: E402, F401
from scap.crosswalk import scap_crosswalk, scap_baseline_compare  # noqa: E402, F401
from scap.threat import scap_attack_correlate  # noqa: E402, F401
from scap.evidence import scap_evidence_pack  # noqa: E402, F401

__all__ = [
    "scap_ingest",
    "scap_query",
    "scap_crosswalk",
    "scap_baseline_compare",
    "scap_attack_correlate",
    "scap_evidence_pack",
]
