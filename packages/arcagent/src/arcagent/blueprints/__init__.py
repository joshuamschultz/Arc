"""SPEC-047 — preset-config blueprints (signed, versioned, materialize-to-disk).

Public surface for the operator CLI (``arc blueprint`` / ``arc init --blueprint``):
discover a preset, verify it fail-closed, deep-merge it UNDER the user's values, and
floor the tier by stringency-max. See :mod:`arcagent.blueprints.loader`.
"""

from __future__ import annotations

from arcagent.blueprints.loader import (
    ResolvedBlueprint,
    apply_blueprint,
    dumps_toml,
    list_blueprints,
    resolve_blueprint,
)

__all__ = [
    "ResolvedBlueprint",
    "apply_blueprint",
    "dumps_toml",
    "list_blueprints",
    "resolve_blueprint",
]
