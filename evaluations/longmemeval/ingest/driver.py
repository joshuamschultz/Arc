"""IngestDriver (COMP-007 / REQ-179, REQ-184, REQ-205).

Feeds one question's chunks to that question's throwaway agent, in order, one
agent turn per chunk. Three properties carry the design:

*A distinct session key per chunk.* ``ingest:<session_idx>:<chunk_idx>``. A
single shared key would replay every prior chunk into the prompt of the next
one — quadratic growth over a 500-turn haystack, ending in a compaction call
that summarizes the haystack before arcmemory ever sees it. Memory scope is
``agent_did``-only, so the split costs nothing at recall (REQ-179).

*Every chunk is screened before the first one is fed.* The fidelity gate is
local CPU and the feed is an LLM call per chunk, so screening the whole
question first means a question the live filters would void costs zero spend
instead of forty calls (REQ-181).

*The background consolidation loop is stopped, not assumed off.* A live
``startup()`` spawns it — the capability loader registers every
``@background_task`` with ``spawn=True``, and only the read-only inventory scan
passes ``spawn=False``. So REQ-184 is something ingest has to *do*.

The report is returned only after the last chunk lands; anything raised on the
way propagates untouched. That is what keeps ``.ingest_complete`` (REQ-205)
honest: its writer never sees a return value for a half-fed workspace, and
chunk-level calls are order-sensitive and not idempotent, so a plausible-looking
partial workspace would silently answer worse than an empty one.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Protocol

from evaluations.longmemeval.ingest.fidelity import GoldEvidenceFilteredError, SanitizeFidelityGate
from evaluations.longmemeval.ingest.types import Chunk

CONSOLIDATE_LOOP_NAME = "memory_consolidate_loop"
"""Registered name of arcmemory's polling loop (``memory.capabilities``)."""


class BackgroundConsolidationError(RuntimeError):
    """The background consolidation loop could not be proven stopped (REQ-184).

    Fatal rather than a warning: arcmemory holds no lock, so a poll landing in
    the middle of a harness-driven pass shares one SQLite connection and one
    manifest file with it, and the corruption surfaces as a bad score.
    """


class IngestTarget(Protocol):
    """The slice of ``ArcAgent`` ingest touches.

    A protocol rather than the concrete class so the driver depends on the two
    members it actually uses. ``_capability_registry`` is private on ``ArcAgent``
    and there is no public accessor for it; reaching it is what lets the loop be
    stopped through the registry's own drain-then-replace path instead of a
    second, harness-local cancellation mechanism.
    """

    _capability_registry: Any

    async def run_collected(self, input_text: str, *, session_key: str) -> Any: ...


@dataclass(frozen=True)
class IngestReport:
    """What one question's ingest actually did.

    ``chunk_hashes`` are SHA-256 of the text as fed, in feed order, so a
    resumed or rebuilt workspace can be compared against the run that filled it
    rather than trusted. ``warnings`` carry non-gold filter damage: real loss,
    but not loss that invalidates the question's score.
    """

    chunks_fed: int
    chunk_hashes: list[str]
    warnings: list[str]


class IngestDriver:
    """Feed a question's chunks to its agent, serially, one turn each."""

    def __init__(self, *, gate: SanitizeFidelityGate) -> None:
        self._gate = gate

    async def ingest(
        self,
        agent: IngestTarget,
        chunks: Sequence[Chunk],
        *,
        gold_turn_ids: set[str],
    ) -> IngestReport:
        """Ingest every chunk in order; return only once all of them landed.

        Serial by construction: each turn is awaited before the next is sent.
        Concurrent turns would interleave arcmemory captures across sessions and
        reorder what its recency channel ranks by (REQ-178).
        """
        warnings = self._screen(chunks, gold_turn_ids)
        await _stop_background_consolidation(agent)

        hashes: list[str] = []
        for chunk in chunks:
            await agent.run_collected(chunk.text, session_key=session_key(chunk))
            hashes.append(hashlib.sha256(chunk.text.encode("utf-8")).hexdigest())
        return IngestReport(chunks_fed=len(hashes), chunk_hashes=hashes, warnings=warnings)

    def _screen(self, chunks: Sequence[Chunk], gold_turn_ids: set[str]) -> list[str]:
        """Run the live filters over every chunk before any of them is fed."""
        warnings: list[str] = []
        for chunk in chunks:
            verdict = self._gate.check(chunk, gold_turn_ids=gold_turn_ids)
            if verdict.gold_overlap:
                raise GoldEvidenceFilteredError(
                    f"the live filters destroyed {verdict.shrunk_by} characters of gold "
                    f"evidence in chunk {chunk.session_idx}:{chunk.chunk_idx} "
                    f"(turns {sorted(gold_turn_ids & set(chunk.turn_ids))})"
                )
            if not verdict.ok:
                warnings.append(
                    f"chunk {chunk.session_idx}:{chunk.chunk_idx}: the live filters "
                    f"destroyed {verdict.shrunk_by} characters of non-gold text"
                )
        return warnings


def session_key(chunk: Chunk) -> str:
    """The chunk's own ingest session key (REQ-179)."""
    return f"ingest:{chunk.session_idx}:{chunk.chunk_idx}"


async def _stop_background_consolidation(agent: IngestTarget) -> None:
    """Cancel arcmemory's polling loop so only the harness fires consolidation.

    ``unregister`` drains the spawned task (cancel, then await) and drops the
    entry, so a later reload cannot resurrect it either. The read-back is the
    fail-closed half: an entry still present means something re-registered the
    loop, and continuing would ingest 500 questions against a corruptible
    manifest while reporting nothing.
    """
    registry = agent._capability_registry
    if registry is None:
        raise BackgroundConsolidationError(
            "agent has no capability registry, so the consolidation loop cannot be "
            f"proven stopped; ingest runs after startup() (REQ-179). "
            f"Expected to unregister {CONSOLIDATE_LOOP_NAME!r}"
        )
    await registry.unregister("background_task", CONSOLIDATE_LOOP_NAME)
    if await registry.get_task(CONSOLIDATE_LOOP_NAME) is not None:
        raise BackgroundConsolidationError(
            f"{CONSOLIDATE_LOOP_NAME!r} is still registered after unregister; the "
            "harness and the background loop would consolidate concurrently and "
            "arcmemory has no lock (REQ-184)"
        )


__all__ = [
    "CONSOLIDATE_LOOP_NAME",
    "BackgroundConsolidationError",
    "IngestDriver",
    "IngestReport",
    "IngestTarget",
    "session_key",
]
