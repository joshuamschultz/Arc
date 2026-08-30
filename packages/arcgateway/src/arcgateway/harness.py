"""``ArcAgentHarness`` — native arcagent as ONE implementation of the seam (H-040 §2.2).

The native agent stops being the hard-coded fleet case and becomes an ordinary
:class:`~arcteam.harness.protocol.HarnessAdapter`, addressed through the same
Protocol as every foreign member. Nothing about arcagent changes on disk or in
behavior (§11) — this is a thin wrapper.

This lives in **arcgateway**, which legally imports both arcagent and arcllm, so
it may map ``arcllm.Delta -> MemberOutput`` at its own edge (§2.4). The Protocol
and the ``MemberOutput`` envelope stay in arcteam, which imports neither. Native
is trusted and may run in-process precisely because ``harness == "arcagent"`` —
decided by the fleet's pinned trust rule (``arcteam.harness.trust``), never a flag.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from typing import Any

import arcrun
from arcteam.harness.protocol import (
    InboundEnvelope,
    MemberOutput,
    MemberStatus,
    MemoryPort,
)
from arcteam.types import Message

_HARNESS = "arcagent"


class ArcAgentHarness:
    """Wrap a started :class:`arcagent.ArcAgent` as a fleet :class:`HarnessAdapter`.

    ``dispatch`` mirrors what the subprocess worker already does —
    ``collect(agent.run(...))`` — and emits the result as ``MemberOutput`` rather
    than ``arcllm.Delta``, keeping the arcteam-owned envelope at this edge.
    """

    def __init__(self, agent: Any) -> None:
        self._agent = agent

    # ---- Identity ----------------------------------------------------------
    @property
    def did(self) -> str:
        return str(self._agent._identity.did)

    @property
    def public_key(self) -> bytes:
        return bytes(self._agent._identity.public_key)

    # ---- Roster / status ---------------------------------------------------
    @property
    def handle(self) -> str:
        return str(self._agent._config.agent.name)

    @property
    def harness(self) -> str:
        return _HARNESS

    async def capabilities(self) -> Sequence[str]:
        # A native agent declares the full capability surface, so it keeps every
        # rich arcagent.toml-driven detail tab (§4). Slice 1 reports the coarse
        # set the fleet routes on; per-tool inventory is Slice 2.
        return ("chat", "tools", "skills", "memory", "workflow")

    async def status(self) -> MemberStatus:
        return MemberStatus(state="online")

    # ---- Run / dispatch (native may run IN-PROCESS — trusted by identity) --
    async def dispatch(self, event: InboundEnvelope) -> AsyncIterator[MemberOutput]:
        """Drive one native turn and emit its result as ``MemberOutput``."""
        session = await self._agent.session(event.session_key or event.member_did)
        result = await arcrun.collect(self._agent.run(event.message.body, session=session))
        yield MemberOutput(kind="text", text=result.content or "", is_final=False)
        yield MemberOutput(kind="done", text="", is_final=True)

    # ---- Message receive ---------------------------------------------------
    async def deliver(self, message: Message) -> None:
        """Route a DM/@mention into the agent's existing messaging inbox loop.

        The native inbox loop (``arcgateway.fleet``) already delivers and wakes
        the agent; this method is the seam's name for that path. When no inbox is
        wired (a bare agent) it is a no-op — the agent runs on normally.
        """
        deliver = getattr(self._agent, "deliver_message", None)
        if deliver is not None:
            await deliver(message)

    # ---- Memory ------------------------------------------------------------
    def memory_port(self) -> MemoryPort | None:
        """Native memory is the agent's own DID-gated ``state()`` guard, untouched.

        The mediated ``MemoryPort`` wrapper is for foreign harnesses (§5); a
        native agent already carries the ContextVar DID gate, so no port is
        imposed here.
        """
        return None


__all__ = ["ArcAgentHarness"]
