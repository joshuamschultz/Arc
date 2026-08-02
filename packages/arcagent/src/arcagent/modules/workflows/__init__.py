"""Workflows module — the agent-facing builder surface for ArcFlow (SPEC-061).

The live surface is the decorator-form capability in :mod:`.capabilities`
(loaded by the capability loader) backed by per-agent runtime state in
:mod:`._runtime`. This module is deliberately THIN: the workflow engine —
models, graph validator, predicate evaluator, definition store, signing gate,
versioning, and the runner — lives in ``arcteam.workflow`` (COMP-001..COMP-008)
behind one shared control plane (COMP-021) that the command line and the
dashboard call too. Nothing here re-implements validation; if a rule exists in
two places it will drift, and the three authoring surfaces must never disagree.

What DOES live here is the part that is genuinely agent-side: field allowlists
on every mutating tool, quota checks before any validation work, Unicode
normalization of inline free text before the injection scan, and the refusal to
report a mutation as anything but a draft.
"""

from __future__ import annotations
