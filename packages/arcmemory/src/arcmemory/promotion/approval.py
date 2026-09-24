"""DID-scoped promotion candidates on the durable operator approval seam."""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Literal

import arctrust
from arcstore.approvals import ApprovalStore, PendingApproval


@dataclass(frozen=True)
class PromotionCandidate:
    """A private source reference and its reviewed digest, without body text."""

    item_id: str
    classification: str
    document_type: str
    digest: str
    effective_score: int


class PromotionApprovalQueue:
    """Keep promotion decisions scoped to one agent and one trusted operator seam."""

    def __init__(
        self,
        store: ApprovalStore,
        *,
        agent_did: str,
        operator_did: str,
        operator_public_key: bytes,
    ) -> None:
        if not agent_did or not operator_did or not operator_public_key:
            raise ValueError("promotion queue requires pinned agent and operator identity")
        self._store = store
        self._agent_did = agent_did
        self._operator_did = operator_did
        self._operator_public_key = operator_public_key

    def _id(self, item_id: str) -> str:
        return "promotion:" + hashlib.sha256(
            f"{self._agent_did}\0{item_id}".encode()
        ).hexdigest()

    def _decision_hash(
        self, *, item_id: str, digest: str, classification: str,
        document_type: str, decision: str,
    ) -> str:
        payload = {
            "agent_did": self._agent_did,
            "item_id": item_id,
            "digest": digest,
            "classification": classification,
            "document_type": document_type,
            "decision": decision,
        }
        return "sha256:" + hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def _verify_grant(
        self, grant: arctrust.ApprovalGrant, *, row: PendingApproval,
        decision: str,
    ) -> bool:
        expected = self._decision_hash(
            item_id=row.arguments["item_id"], digest=row.arguments["digest"],
            classification=row.arguments["classification"],
            document_type=row.arguments["document_type"], decision=decision,
        )
        return (
            row.agent_did == self._agent_did
            and row.tool == "memory.promotion"
            and grant.approver_did == self._operator_did
            and grant.public_key == self._operator_public_key
            and arctrust.verify_approval_for_hash(
                expected, grant, agent_did=self._agent_did
            )
        )

    async def enqueue(
        self,
        *,
        item_id: str,
        title: str,
        content: str,
        classification: str,
        document_type: str,
        digest: str,
        effective_score: int,
        access: object,
    ) -> bool:
        if getattr(access, "caller_did", None) != self._agent_did:
            raise PermissionError("promotion candidate agent scope mismatch")
        if not item_id or len(item_id) > 200 or len(content) > 80_000:
            raise ValueError("promotion candidate exceeds review limits")
        if "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
            raise ValueError("promotion candidate digest mismatch")
        approval_id = self._id(item_id)
        if await self._store.get(approval_id) is not None:
            return False
        await self._store.create(
            PendingApproval(
                id=approval_id,
                agent_did=self._agent_did,
                tool="memory.promotion",
                call_hash=self._decision_hash(
                    item_id=item_id, digest=digest,
                    classification=classification,
                    document_type=document_type, decision="approve",
                ),
                arguments={
                    "item_id": item_id,
                    "classification": classification,
                    "document_type": document_type,
                    "digest": digest,
                    "effective_score": str(effective_score),
                },
            )
        )
        return True

    async def _list(self, status: str) -> list[PromotionCandidate]:
        rows = await self._store.list(status=status)
        return [
            PromotionCandidate(
                item_id=row.arguments["item_id"],
                classification=row.arguments["classification"],
                document_type=row.arguments["document_type"],
                digest=row.arguments["digest"],
                effective_score=int(row.arguments["effective_score"]),
            )
            for row in rows
            if row.agent_did == self._agent_did and row.tool == "memory.promotion"
            and (status != "approved" or self._approved_grant_valid(row))
        ]

    def _approved_grant_valid(self, row: PendingApproval) -> bool:
        if row.grant is None:
            raise PermissionError("approved promotion lacks operator proof")
        try:
            valid = self._verify_grant(
                arctrust.grant_from_wire(row.grant), row=row, decision="approve"
            )
        except (KeyError, ValueError, TypeError) as error:
            raise PermissionError("invalid promotion operator proof") from error
        if not valid:
            raise PermissionError("invalid promotion operator proof")
        return True

    async def list_pending(self) -> list[PromotionCandidate]:
        """Return this agent's undecided candidates."""
        return await self._list("pending")

    async def list_approved(self) -> list[PromotionCandidate]:
        """Return this agent's approved, unreleased candidates."""
        return await self._list("approved")

    async def list_uncertain(self) -> list[PromotionCandidate]:
        """Return claims needing operator reconciliation; never auto-retry them."""
        return [*await self._list("releasing"), *await self._list("outcome_unknown")]

    async def approve(self, item_id: str, *, grant: arctrust.ApprovalGrant) -> None:
        """Accept an exact signed operator decision for this source revision."""
        row = await self._store.get(self._id(item_id))
        if row is None or not self._verify_grant(grant, row=row, decision="approve"):
            raise PermissionError("operator promotion authority denied")
        if await self._store.resolve(
            self._id(item_id), status="approved", actor_did=grant.approver_did,
            resolved_by=grant.approver_did, grant=arctrust.grant_to_wire(grant),
        ) is None:
            raise ValueError("promotion candidate is no longer pending")

    async def deny(self, item_id: str, *, grant: arctrust.ApprovalGrant) -> None:
        """Remember a signed denial so consolidation cannot requeue it."""
        row = await self._store.get(self._id(item_id))
        if row is None or not self._verify_grant(grant, row=row, decision="deny"):
            raise PermissionError("operator promotion authority denied")
        if await self._store.resolve(
            self._id(item_id), status="denied", actor_did=grant.approver_did,
            resolved_by=grant.approver_did, grant=arctrust.grant_to_wire(grant),
        ) is None:
            raise ValueError("promotion candidate is no longer pending")

    async def claim_release(self, item_id: str) -> str | None:
        """Return a private worker token only when this worker wins the release CAS."""
        token = secrets.token_urlsafe(24)
        won = await self._store.claim_release(
            self._id(item_id), actor_did=self._agent_did, token=token
        )
        return token if won else None

    async def finish_release(
        self, item_id: str, *, token: str, outcome: Literal["released", "outcome_unknown"]
    ) -> None:
        """Record completion or an uncertain effect without reopening the claim."""
        if outcome not in {"released", "outcome_unknown"}:
            raise ValueError("invalid promotion release outcome")
        if not await self._store.finish_release(
            self._id(item_id), actor_did=self._agent_did, token=token,
            outcome=outcome,
        ):
            raise ValueError("promotion release claim was lost")


__all__ = ["PromotionApprovalQueue", "PromotionCandidate"]
