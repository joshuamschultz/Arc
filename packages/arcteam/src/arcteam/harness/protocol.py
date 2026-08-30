"""The one contract a fleet needs FROM a member — ``HarnessAdapter`` (H-040 §2.1).

A fleet member is anything that can answer these questions and accept these
verbs. Native ``arcagent`` becomes ONE implementation of this seam (wrapped by
``ArcAgentHarness`` in arcgateway); a foreign harness (hermes, openclaw, …) is
another. The Protocol lives in **arcteam** and speaks only arcteam + arctrust
types — it must never import arcagent or arcllm (the illegal upward edge that the
layering tests guard). That is why ``dispatch`` yields the arcteam-owned
:class:`MemberOutput` envelope and NOT arcllm's ``Delta`` (§2.4).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict

from arcteam.types import Message


class MemberOutput(BaseModel):
    """One streamed chunk a member emits in reply to a dispatched turn.

    An arcteam-owned envelope mirroring the JSON-lines shape the subprocess
    worker protocol already speaks (a ``kind`` + text + ``is_final`` flag) —
    deliberately NOT ``arcllm.Delta``, because arcteam may not import arcllm.
    ``ArcAgentHarness`` (in arcgateway) maps ``arcllm.Delta -> MemberOutput`` at
    its own edge; a foreign adapter emits ``MemberOutput`` directly.
    """

    model_config = ConfigDict(frozen=True)

    kind: Literal["text", "done", "error"]
    text: str = ""
    is_final: bool = False


class MemberStatus(BaseModel):
    """Roster status of a member — for the fleet card and policy tags."""

    model_config = ConfigDict(frozen=True)

    state: Literal["online", "idle", "offline"]
    detail: str = ""


class InboundEnvelope(BaseModel):
    """What the fleet hands a member's :meth:`HarnessAdapter.dispatch`.

    Mirrors ``arc-agent-worker``'s ``InboundEvent``: the routed message plus the
    member DID it is addressed to and a session key for turn continuity. A
    foreign harness maps this onto its own turn.
    """

    model_config = ConfigDict(frozen=True)

    message: Message
    member_did: str
    session_key: str = ""


class Degraded(BaseModel):
    """A typed 'this harness cannot serve that verb' result — never a crash.

    A foreign harness will not support every capability (no skills surface, no
    private memory, no workflow authoring). The seam handles absence the way arc
    already does: a typed degraded result, not an exception and not a silent
    empty, so the fleet renders "capability X not supported by this harness" and
    any authorization check for the absent capability is a fail-closed DENY
    (H-040 §2.1).
    """

    model_config = ConfigDict(frozen=True)

    capability: str
    reason: str


@runtime_checkable
class MemoryPort(Protocol):
    """DID-scoped memory access handed to a member (H-040 §5).

    Slice 1 exposes only the shared-read path (via ``TeamMemoryService``, already
    harness-agnostic); the DID-gated PRIVATE port is Slice 2. A member never gets
    raw ``Brain`` / ``build_brain`` — the port owns its DID and workspace so a
    foreign harness cannot name another agent's memory.
    """

    @property
    def member_did(self) -> str: ...


@runtime_checkable
class HarnessAdapter(Protocol):
    """What a fleet needs FROM a member to treat it as first-class.

    Inverse of ``arcagent.fleet.FleetProvider`` (what a member needs FROM the
    fleet). Native arcagent implements BOTH: it consumes ``FleetProvider`` and it
    is wrapped by an ``ArcAgentHarness`` that satisfies this.
    """

    # ---- Identity ----------------------------------------------------------
    @property
    def did(self) -> str: ...

    @property
    def public_key(self) -> bytes: ...

    # ---- Roster / status ---------------------------------------------------
    @property
    def handle(self) -> str: ...

    @property
    def harness(self) -> str: ...  # "arcagent" | "hermes" | ...

    async def capabilities(self) -> Sequence[str]: ...

    async def status(self) -> MemberStatus: ...

    # ---- Run / dispatch ----------------------------------------------------
    def dispatch(self, event: InboundEnvelope) -> AsyncIterator[MemberOutput]: ...

    # ---- Message receive ---------------------------------------------------
    async def deliver(self, message: Message) -> None: ...

    # ---- Memory (mediated, never raw Brain) --------------------------------
    def memory_port(self) -> MemoryPort | None: ...


__all__ = [
    "Degraded",
    "HarnessAdapter",
    "InboundEnvelope",
    "MemberOutput",
    "MemberStatus",
    "MemoryPort",
]
