"""Tasks module — mission-control task directory tools (SPEC-056 Phase B).

The live surface is the decorator-form capability in :mod:`.capabilities`
(loaded by the capability loader) backed by per-agent runtime state in
:mod:`._runtime`. Durable storage lives in the arcstore ``tasks`` collection
(SPEC-056 Phase A) — this module is a thin tool surface over it.
"""

from __future__ import annotations
