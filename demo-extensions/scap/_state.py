"""Module-level ingest cache (SPEC-024 § SDD §2, D-378).

Mirrors the `_runtime` pattern used by arcagent built-in tools: stateless
public tool functions that read/write a private module-level dict. Cache
is per-process — lost when the agent restarts. That's intentional for
the demo (9-min run); cross-restart persistence is post-NLIT scope.
"""

from __future__ import annotations

from .models import IngestResult

_INGESTS: dict[str, IngestResult] = {}


def get(alias: str) -> IngestResult | None:
    return _INGESTS.get(alias)


def put(alias: str, result: IngestResult) -> None:
    _INGESTS[alias] = result


def all() -> list[IngestResult]:
    return list(_INGESTS.values())


def aliases() -> list[str]:
    return list(_INGESTS.keys())


def clear() -> None:
    _INGESTS.clear()
