"""The one seam every corpus attaches to (COMP-001 / REQ-174).

One method is the whole contract. A corpus adapter yields ``Session`` objects
and nothing else, so adding a corpus adds exactly one module and touches
neither the chunker, the ingest driver, nor the consolidation waiter.

``runtime_checkable`` because the harness asserts an adapter satisfies the seam
before a run rather than discovering a missing ``read`` mid-ingest. Structural
typing (not a base class) is deliberate: an adapter for a future corpus need
not import this package to conform, which is what keeps the seam one-way.

This module imports nothing from ``evaluations.longmemeval`` — that absence is
enforced by ``tests/architecture/test_no_evaluations_layering_violations.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from evaluations.longmemeval.ingest.types import Session


@runtime_checkable
class SourceAdapter(Protocol):
    """A corpus, exposed as sessions in the corpus's own pinned order."""

    def read(self) -> Iterator[Session]:
        """Yield every session of the corpus, never reordered (REQ-178)."""
        ...
