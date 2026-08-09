"""ToolContractLedger — the rug-pull defence (SPEC-062 COMP-007, REQ-291).

Pinning an artifact proves the *code* is the code that was approved. It says
nothing about the tool list that code serves, and a hosted attachment has no
artifact to pin at all — so an upstream may serve a benign contract at approval
time and a different one on the next retrieval (CVE-2025-54136). The protocol
offers no continuous re-verification, which leaves this ledger as the only
defence covering every attachment shape.

The control is **suspension, not detection**. A component that notices the
change, logs it, and still lets the call through is the vulnerability wearing
the mitigation's clothes, so a changed contract makes the tool uncallable until
an operator approves it again.

Three decisions carry weight:

* **Approval is the only writer.** :meth:`ToolContractLedger.review` never
  records a hash. Recording on review — in either direction, first sighting or
  repeat — would bless whatever the upstream currently serves and delete the
  defence while leaving its tests green.
* **Upstream annotations stay out of the hash.** ``classification`` and
  ``capability_tags`` come from the manifest, not the server, per MCP guidance
  on untrusted upstreams. If they were hashed, any upstream could force an
  approval prompt at will, and an operator trained to click through prompts is
  the real exploit.
* **Hashing goes through** :func:`~arctrust.canonical.canonical_json`, the byte
  form every other Arc signature commits to. Key order therefore cannot make a
  semantically identical schema read as a change, which is what keeps false
  suspensions — and the click-through habit they teach — from happening.

Approved hashes live in the connection store (COMP-019), not in memory: a
suspension that clears on the next agent start clears at exactly the moment a
poisoned description gets its chance to run.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from enum import StrEnum

from arctrust.audit import AuditEvent, AuditSink, emit
from arctrust.canonical import canonical_json

from arcagent.core.errors import ExtensionError
from arcagent.extension.attachment import ToolSpec
from arcagent.extension.state import ConnectionStateStore

#: Refusal code for an approval that reached the store and changed nothing. A
#: surface renders it as "that connection is not registered", never as success.
APPROVAL_NOT_STORED = "CONNECTION_APPROVAL_NOT_STORED"

#: A suspension is the ledger's own act, not an agent's or an operator's — no
#: one requested it, a mismatch caused it. Naming the component follows the
#: pattern ``arctrust.audit.WormSink`` already uses for its recovery records.
LEDGER_DID = "did:arc:arcagent:contract-ledger"

_ACTION_PREFIX = "connector.tool.contract"


class ContractVerdict(StrEnum):
    """What a served tool contract turned out to be, relative to what was approved."""

    UNCHANGED = "unchanged"
    SUSPENDED = "suspended"
    NEW = "new"


def contract_hash(spec: ToolSpec) -> str:
    """Hash the part of a tool the model reads and the caller is bound by (REQ-291).

    Name, description, and input schema: the description is where the classic
    poisoning payload lives, and the schema is where a new parameter asks for
    something the operator never approved handing over. Nothing the upstream
    merely annotates is included — see the module docstring.
    """
    return hashlib.sha256(
        canonical_json(
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
            }
        )
    ).hexdigest()


def _verdict_for(served_hash: str, approved_hash: str | None) -> ContractVerdict:
    """Judge one served contract. Pure — the recording happens at the call site."""
    if approved_hash is None:
        return ContractVerdict.NEW
    if approved_hash == served_hash:
        return ContractVerdict.UNCHANGED
    return ContractVerdict.SUSPENDED


class ToolContractLedger:
    """Approves, re-verifies, and suspends the tool contracts of one connection.

    Args:
        store: The connection state store holding this connection's approved hashes.
        connection: The connected account these contracts belong to. Not an
            agent: the contract is what the upstream serves, so every agent
            granted this connection is bound by one approval and one suspension.
        sink: Where approvals and suspensions are recorded; every event reaches it
            through the single :func:`~arctrust.audit.emit` chokepoint (CON-5).
    """

    def __init__(
        self,
        store: ConnectionStateStore,
        *,
        connection: str,
        sink: AuditSink,
    ) -> None:
        self._store = store
        self._connection = connection
        self._sink = sink

    async def approve(self, specs: Sequence[ToolSpec], *, actor_did: str) -> None:
        """Record each contract as approved by ``actor_did`` (REQ-291).

        The only path that writes a hash, and therefore the only path that can
        clear a suspension — re-approving a changed tool is a deliberate act by
        a named operator, never a side effect of looking at the tool list.

        Raises:
            ExtensionError: The hash was not stored, which the store reports by
                returning False from a merge against a connection it does not
                hold. Raising is the whole point: an ``approved`` event for a
                write that did nothing is an audit trail recording approvals
                that never happened, and it is worse than no audit trail — it is
                what let ``Approved 5 tool contract(s)`` print over an empty
                store while every one of those tools stayed uncallable.
        """
        for spec in specs:
            served = contract_hash(spec)
            stored = await self._store.approve_tool_contract(
                self._connection, spec.name, served, actor_did=actor_did
            )
            if not stored:
                raise ExtensionError(
                    code=APPROVAL_NOT_STORED,
                    message=(
                        f"{self._connection!r} is not a registered connection, "
                        f"so the contract for {spec.name!r} could not be approved"
                    ),
                    details={"connection": self._connection, "tool": spec.name},
                )
            self._emit(
                actor_did=actor_did,
                action=f"{_ACTION_PREFIX}.approve",
                tool=spec.name,
                outcome="approved",
                extra={"approved_hash": served},
            )

    async def review(self, specs: Sequence[ToolSpec]) -> dict[str, ContractVerdict]:
        """Re-verify a served tool list against what was approved. Never writes.

        Returns one verdict per served tool. Anything other than
        :attr:`ContractVerdict.UNCHANGED` is recorded as it is found, so the
        audit trail names the tool whose contract moved and the connection it
        was served on.
        """
        approved = await self._approved_hashes()
        verdicts: dict[str, ContractVerdict] = {}
        for spec in specs:
            served = contract_hash(spec)
            expected = approved.get(spec.name)
            verdict = _verdict_for(served, expected)
            if verdict is not ContractVerdict.UNCHANGED:
                self._record(spec.name, verdict, served=served, approved=expected)
            verdicts[spec.name] = verdict
        return verdicts

    async def callable_tools(self, specs: Sequence[ToolSpec]) -> list[ToolSpec]:
        """The subset of a served list that may actually be called (REQ-291).

        Suspension is enforced here, by omission: a tool whose contract changed,
        and a tool nobody ever approved, are both simply absent. Blast radius is
        one tool — a whole connection going dark is its own outage.
        """
        verdicts = await self.review(specs)
        return [spec for spec in specs if verdicts[spec.name] is ContractVerdict.UNCHANGED]

    async def _approved_hashes(self) -> dict[str, str]:
        """Every approved hash for this connection, in one read of the durable row."""
        record = await self._store.get(self._connection)
        return dict(record.approved_tool_hashes) if record is not None else {}

    def _record(
        self,
        tool: str,
        verdict: ContractVerdict,
        *,
        served: str,
        approved: str | None,
    ) -> None:
        """Audit a tool that is not callable, naming both hashes for reconstruction."""
        action = "suspend" if verdict is ContractVerdict.SUSPENDED else "unapproved"
        self._emit(
            actor_did=LEDGER_DID,
            action=f"{_ACTION_PREFIX}.{action}",
            tool=tool,
            outcome=verdict.value,
            extra={"approved_hash": approved, "served_hash": served},
        )

    def _emit(
        self,
        *,
        actor_did: str,
        action: str,
        tool: str,
        outcome: str,
        extra: dict[str, str | None],
    ) -> None:
        """Hand one record to the single emission point (CON-5)."""
        emit(
            AuditEvent(
                actor_did=actor_did,
                action=action,
                target=f"connector:{self._connection}:{tool}",
                outcome=outcome,
                extra={"connection": self._connection, "tool": tool, **extra},
            ),
            self._sink,
        )


__all__ = [
    "APPROVAL_NOT_STORED",
    "LEDGER_DID",
    "ContractVerdict",
    "ToolContractLedger",
    "contract_hash",
]
