"""PromptSnapshot + run-start provenance audit (COMP-004).

At run start the full prompt set is resolved *once* and frozen for the run's
duration (REQ-123): every turn observes the identical snapshot even if an
overlay file changes mid-run; the next run picks up the change. The freeze is
what makes a run's behavior attributable to exact bytes — a per-turn re-read
could shift a prompt between turns with no single version to attribute.

Snapshotting emits exactly one audit event (REQ-132) enumerating every prompt
with its package, name, resolution source, and sha256, plus the resolved signer
DID for each overlay. The event carries *resolved* values only — never a
hardcoded tier or default signer (the SPEC-017 audit-lies regression).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from arctrust.audit import AuditEvent, AuditSink, emit

from arcprompt.catalog import PromptRef
from arcprompt.document import PromptDocument
from arcprompt.resolver import PromptResolver

PROVENANCE_ACTION = "prompt.snapshot"


class PromptSnapshot:
    """Immutable per-run mapping of ``(package, name)`` to its resolved document."""

    def __init__(self, entries: Mapping[tuple[str, str], PromptDocument]) -> None:
        self._entries: dict[tuple[str, str], PromptDocument] = dict(entries)

    def get(self, package: str, name: str) -> PromptDocument:
        """Return the frozen document resolved for this run, or raise KeyError."""
        return self._entries[(package, name)]

    def __contains__(self, key: tuple[str, str]) -> bool:
        return key in self._entries

    def __len__(self) -> int:
        return len(self._entries)

    def items(self) -> list[tuple[tuple[str, str], PromptDocument]]:
        return list(self._entries.items())


def _provenance_entries(snapshot: PromptSnapshot) -> list[dict[str, str | None]]:
    """Build the per-prompt provenance rows from resolved documents only."""
    rows: list[dict[str, str | None]] = []
    for (package, name), doc in sorted(snapshot.items(), key=lambda kv: kv[0]):
        rows.append(
            {
                "package": package,
                "name": name,
                "source": doc.source,
                "sha256": doc.sha256,
                "signer_did": doc.signer_did,
            }
        )
    return rows


def snapshot(
    resolver: PromptResolver,
    refs: Sequence[PromptRef],
    *,
    actor_did: str,
    sink: AuditSink,
    request_id: str | None = None,
) -> PromptSnapshot:
    """Resolve and freeze every prompt in ``refs``; emit one provenance event.

    The event's ``tier`` is the resolver's held posture and each row's
    ``signer_did`` is the overlay's real signer — both resolved, never defaulted.
    """
    entries = {(ref.package, ref.name): resolver.resolve(ref.package, ref.name) for ref in refs}
    snap = PromptSnapshot(entries)
    emit(
        AuditEvent(
            actor_did=actor_did,
            action=PROVENANCE_ACTION,
            target="run",
            outcome="resolved",
            tier=resolver.posture.value,
            request_id=request_id,
            extra={"prompts": _provenance_entries(snap)},
        ),
        sink,
    )
    return snap


__all__ = [
    "PROVENANCE_ACTION",
    "PromptSnapshot",
    "snapshot",
]
