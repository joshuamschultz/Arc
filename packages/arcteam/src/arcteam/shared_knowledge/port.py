"""The fleet's shared-knowledge port, bound to one composed agent (SPEC-083 COMP-023).

Structurally an ``arcagent.knowledge.SharedKnowledgePort``. The fleet binds one
per started agent — same service, the agent's own access and signer, and its
audit sink — and hands it to the agent through
``ArcAgent.attach_shared_knowledge``. A module that wants it (memory promotion)
subscribes; nothing in arcagent core names this class.

The port authorizes nothing itself: every check (owner == caller, clearance,
no-write-down, signer bound to the DID, TOFU pin, type allowlist, classifier
decision shape, durable decision audit) stays in
:meth:`FleetSharedKnowledgeService.promote`. A port whose bound access is not the
caller refuses, so it cannot be used as a confused deputy for another DID.
"""

from __future__ import annotations

from typing import Any, Protocol

from arctrust.classification import parse_classification

from arcteam.shared_knowledge.service import FleetSharedKnowledgeService


class _Access(Protocol):
    @property
    def caller_did(self) -> str: ...

    @property
    def clearance(self) -> str: ...


class _Signer(Protocol):
    @property
    def public_key(self) -> bytes: ...

    @property
    def algorithm(self) -> str: ...

    def sign(self, message: bytes) -> bytes: ...


class _Source(Protocol):
    @property
    def reference(self) -> Any: ...


class _GivenSource:
    """Hands the service the one source the caller already verified.

    The service re-hashes the content against the digest itself, so this adds
    no trust; it only lets the promote path pull through its usual exporter seam.
    """

    def __init__(self, source: _Source) -> None:
        self._source = source

    async def export_for_promotion(self, reference: str, access: _Access) -> _Source:
        return self._source


class FleetSharedKnowledgePort:
    """One agent's view of the fleet's signed shared-knowledge collection."""

    def __init__(
        self,
        service: FleetSharedKnowledgeService,
        *,
        access: _Access,
        signer: _Signer,
        audit_sink: Any = None,
    ) -> None:
        self._service = service
        self._access = access
        self._signer = signer
        self._audit_sink = audit_sink

    async def promote(
        self,
        source: _Source,
        access: _Access,
        *,
        decision: str,
        confidence: float,
        classifier_version: str,
    ) -> Any:
        """Promote ``source`` as the bound agent under a classifier decision."""
        self._require_bound_caller(access)
        return await self._service.promote(
            _GivenSource(source),
            str(source.reference.identifier),
            access,
            self._signer,
            audit_sink=self._audit_sink,
            decision=decision,
            confidence=confidence,
            classifier_version=classifier_version,
        )

    async def save(self, draft: object, access: _Access) -> Any:
        """Refused: shared knowledge is written only by promotion from personal scope."""
        raise PermissionError("shared knowledge is written only by promotion")

    async def read(self, reference: str, access: _Access) -> Any:
        self._require_bound_caller(access)
        return await self._service.read(reference, access)

    async def search(self, query: str, access: _Access) -> list[Any]:
        self._require_bound_caller(access)
        return await self._service.search(query, access)

    async def revoke(self, reference: str, access: _Access) -> None:
        self._require_bound_caller(access)
        await self._service.revoke(reference, access)

    def _require_bound_caller(self, access: _Access) -> None:
        """Refuse any caller but the bound agent at its bound clearance level.

        Levels compare parsed (strict), so ``unclassified`` == ``UNCLASSIFIED``
        while a claimed higher clearance is refused. An unparseable label is a
        refusal too (``PermissionError``, the seam's "nothing written" contract).
        """
        try:
            same_level = parse_classification(
                access.clearance, strict=True
            ) == parse_classification(self._access.clearance, strict=True)
        except ValueError as error:
            raise PermissionError("shared knowledge port refused an unknown clearance") from error
        if access.caller_did != self._access.caller_did or not same_level:
            raise PermissionError("shared knowledge port is bound to a different caller")


__all__ = ["FleetSharedKnowledgePort"]
