"""``HermesHarness`` — the Slice-1 reference foreign member (H-040 item 6).

A read-only foreign harness that can be enrolled, appears in the roster with its
``hermes`` badge, receives an ``@mention``/DM, and delivers a reply — proving the
:class:`~arcteam.harness.protocol.HarnessAdapter` seam end to end. It RUNS
OUT-OF-PROCESS (Posture A, §9): every turn is dispatched to a subprocess
(:mod:`arcteam.harness.types.hermes.worker`) Arc spawns and owns, so a foreign
runtime never shares the fleet process — the memory-isolation guarantee (§5)
depends on this even in the first slice.

It imports only arcteam types + stdlib — never arcagent or arcllm. Enforcement
and audit run ARC-SIDE: the reply goes back through the fleet ``MessagingService``
(signed, audited), never self-enforced or self-audited by the subprocess (§10).
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import AsyncIterator, Sequence

from arcteam.harness.protocol import (
    InboundEnvelope,
    MemberOutput,
    MemberStatus,
    MemoryPort,
)
from arcteam.types import Entity, Message

_WORKER_MODULE = "arcteam.harness.types.hermes.worker"
_HARNESS = "hermes"


class HermesHarness:
    """A read-only foreign fleet member, dispatched out-of-process.

    Constructed by the ``hermes`` :class:`~arcteam.harness.agent_type.AgentType`
    after enrollment verification (chokepoint 3). Holds the member's own identity
    (Option A self-sign, §3.6) and the fleet messenger it replies through.
    """

    def __init__(
        self,
        *,
        entity: Entity,
        messenger: object | None = None,
        python_executable: str | None = None,
    ) -> None:
        self._entity = entity
        # The fleet MessagingService (signed as this member) used to post replies
        # back into the origin channel. Optional so the harness can dispatch
        # without a live bus (unit tests drive dispatch directly).
        self._messenger = messenger
        self._python = python_executable or sys.executable

    # ---- Identity ----------------------------------------------------------
    @property
    def did(self) -> str:
        return self._entity.did

    @property
    def public_key(self) -> bytes:
        return bytes.fromhex(self._entity.public_key)

    # ---- Roster / status ---------------------------------------------------
    @property
    def handle(self) -> str:
        return self._entity.handle

    @property
    def harness(self) -> str:
        return _HARNESS

    async def capabilities(self) -> Sequence[str]:
        return ("chat",)  # read-only: it can be messaged and reply, nothing more

    async def status(self) -> MemberStatus:
        return MemberStatus(state="online")

    # ---- Run / dispatch (OUT-OF-PROCESS, Posture A) ------------------------
    async def dispatch(self, event: InboundEnvelope) -> AsyncIterator[MemberOutput]:
        """Run one turn in a subprocess Arc spawns and owns; yield its output.

        The subprocess is the foreign runtime's isolation boundary — its own
        process, its own memory, no access to the fleet's ``build_brain`` (§5.2).
        """
        proc = await asyncio.create_subprocess_exec(
            self._python,
            "-m",
            _WORKER_MODULE,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        assert proc.stdin is not None and proc.stdout is not None  # noqa: S101  # PIPE guarantees both
        payload = json.dumps({"message": event.message.body, "handle": self._entity.handle})
        proc.stdin.write((payload + "\n").encode("utf-8"))
        await proc.stdin.drain()
        proc.stdin.close()
        try:
            while True:
                line_bytes = await proc.stdout.readline()
                if not line_bytes:
                    break
                line = line_bytes.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                yield MemberOutput.model_validate_json(line)
        finally:
            await proc.wait()

    # ---- Message receive ---------------------------------------------------
    async def deliver(self, message: Message) -> None:
        """A DM/@mention the fleet routed here: run a turn and post the reply.

        The reply travels back through the fleet ``MessagingService`` — signed as
        this member and audited arc-side (§6/§10). A read-only member never writes
        anything else.
        """
        envelope = InboundEnvelope(message=message, member_did=self._entity.did)
        parts: list[str] = []
        async for out in self.dispatch(envelope):
            if out.kind == "text":
                parts.append(out.text)
        reply = "".join(parts)
        if self._messenger is not None and reply:
            await self._messenger.send(  # type: ignore[attr-defined]  # duck-typed MessagingService
                Message(
                    sender=self._entity.handle,
                    to=[message.sender],
                    body=reply,
                    classification=message.classification,
                )
            )

    # ---- Memory (Slice 1: shared-read only, no private port) ---------------
    def memory_port(self) -> MemoryPort | None:
        """No private port in Slice 1 — shared reads go via ``TeamMemoryService``.

        The DID-gated private ``MemoryPort`` is Slice 2; until then a foreign
        member has no private memory access at all (fail-closed by absence).
        """
        return None


__all__ = ["HermesHarness"]
