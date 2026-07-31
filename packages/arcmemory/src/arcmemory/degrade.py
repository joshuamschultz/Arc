"""Loud, once-per-process signalling that the semantic channel went dark.

arcmemory is *designed* to degrade: with no vector channel, BM25 + graph still
answer and nothing raises (REQ-041). The failure mode this module closes is that
the degrade used to be **invisible** — the default ``embed_backend = "local"`` with
the embedder package absent produced a recall path that looked healthy while its
semantic third contributed nothing, discoverable only by grepping an audit log.

Two rules make it operator-visible without becoming noise:

* **Loud** — a ``WARNING`` on the ``arcmemory.degrade`` logger naming the dead
  channel and the fix, plus a process-wide flag (:func:`semantic_degraded`) the
  ``arc memory status`` readout reads directly.
* **Once** — keyed by *reason*, so twenty queries against a dead embedder produce
  one line, not twenty. A per-query log flood is its own bug.

Every key here is a semantic-degrade reason; the per-query ``recall.degraded``
audit event (emitted in ``index/surface.py`` and ``index/structural.py``) remains
the compliance trail and is unaffected.
"""

from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_warned: set[str] = set()


def warn_once(key: str, message: str) -> bool:
    """Log ``message`` at WARNING the first time ``key`` is seen; return whether it did.

    Thread-safe: concurrent agents sharing a process emit exactly one line per
    reason between them.
    """
    with _lock:
        if key in _warned:
            return False
        _warned.add(key)
    logger.warning("%s", message)
    return True


def semantic_degraded() -> bool:
    """True once any semantic-degrade reason has fired in this process."""
    return bool(_warned)


def degraded_reasons() -> set[str]:
    """The reason keys that have fired — what an operator has to fix."""
    return set(_warned)


def reset_degrade_warnings() -> None:
    """Re-arm every warning (test isolation; the flag is process-global)."""
    with _lock:
        _warned.clear()


__all__ = ["degraded_reasons", "reset_degrade_warnings", "semantic_degraded", "warn_once"]
