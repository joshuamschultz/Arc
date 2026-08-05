"""SPEC-062 COMP-004 — ``ExtensionAttachment``, the single hook an extension plugs into.

This is the seam the whole spec exists to build. A protocol-server extension, a
direct-implementation extension, and a skills-only extension all satisfy the same
four methods, so adding the eleventh connection means writing an implementation —
never editing a file in ``arcagent`` (REQ-278, REQ-280).

The contract therefore names nothing concrete: no vendor, no service, no wire
protocol, no transport. It speaks only its own value types, and core depends on
those and on this Protocol. An implementation lives outside core and is reached
through the loader.

The four methods split by what they cost:

* :meth:`ExtensionAttachment.requirements` — a pure declaration of what the host
  machine and the operator must supply. Arc directs the operator to satisfy it and
  never installs it on their behalf (REQ-262), so this method performs no I/O.
* :meth:`ExtensionAttachment.probe` — proves the connection actually works.
* :meth:`ExtensionAttachment.describe_tools` — the live tool list.
* :meth:`ExtensionAttachment.invoke` — one call, one result.

The last three reach an external system and are therefore ``async``: every
attachment shipped so far is I/O-bound, and a synchronous method would put that I/O
on the event loop.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

#: What a tool is allowed to do. An absent declaration defaults to the more
#: restrictive value everywhere it is read (REQ-269) — silence never buys a tool
#: the cheap treatment, because classification is what lets the loop parallelize.
Classification = Literal["read_only", "state_modifying"]


class _Contract(BaseModel):
    """Base for the hook's value types: frozen, and a typo is an error not a shrug."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RequirementKind(StrEnum):
    """Who has to satisfy a requirement before an attachment can connect."""

    HOST = "host"
    CREDENTIAL = "credential"


class Requirement(_Contract):
    """One thing that must exist before this attachment can connect.

    Attributes:
        kind: Whether the host machine or the operator supplies it.
        name: The binary, runtime, or credential key being required.
        minimum_version: Lowest acceptable version, when the requirement is versioned.
        instruction: How the operator satisfies it. Arc shows this rather than
            installing anything itself (REQ-262).
    """

    kind: RequirementKind
    name: str
    minimum_version: str | None = None
    instruction: str = ""


class ToolSpec(_Contract):
    """One tool an attachment offers, in the shape the registry bridge registers.

    ``classification`` and ``capability_tags`` are what feed the trifecta gate, so
    they travel with the tool rather than being inferred at the call site.
    """

    name: str
    description: str = ""
    input_schema: dict[str, Any] = Field(default_factory=dict)
    classification: Classification = "state_modifying"
    capability_tags: list[str] = Field(default_factory=list)


class ProbeResult(_Contract):
    """The verdict of a connection test: did it answer, and with which tools."""

    reachable: bool
    tools: list[ToolSpec] = Field(default_factory=list)
    detail: str = ""


class ToolOutcome(StrEnum):
    """How one call ended.

    ``ERROR`` is a tool failure the agent can read and reason about — distinct from
    a transport or protocol failure, which raises instead. ``INPUT_REQUIRED`` is a
    round trip that needs something more from the operator and must never be
    mistaken for completion.
    """

    OK = "ok"
    ERROR = "error"
    INPUT_REQUIRED = "input_required"


class ToolResult(_Contract):
    """The result of one call. ``content`` carries the failure text when not ``OK``."""

    tool: str
    outcome: ToolOutcome = ToolOutcome.OK
    content: str = ""


@runtime_checkable
class ExtensionAttachment(Protocol):
    """How an extension attaches an external system to an agent.

    ``runtime_checkable`` so the loader can fail closed on an extension factory that
    returns something which does not satisfy the hook, rather than discovering it at
    the first tool call.
    """

    def requirements(self) -> list[Requirement]:
        """Declare what the host machine and the operator must supply."""
        ...

    async def probe(self) -> ProbeResult:
        """Prove the connection works and report the live tool list."""
        ...

    async def describe_tools(self) -> list[ToolSpec]:
        """Return the tools this attachment offers."""
        ...

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute one call against ``tool`` with already-validated ``args``."""
        ...


__all__ = [
    "Classification",
    "ExtensionAttachment",
    "ProbeResult",
    "Requirement",
    "RequirementKind",
    "ToolOutcome",
    "ToolResult",
    "ToolSpec",
]
