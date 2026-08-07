"""The reference extension — SPEC-062 COMP-021, the fixture T-915 is written around.

This file is the whole third-party side of the mechanism claim. It implements
:class:`~arcagent.extension.attachment.ExtensionAttachment` and imports nothing else
from Arc: no registry, no bridge, no loader, no module. If adding a connection needs
more than this file plus an ``extension.toml``, the hook is not the seam REQ-278 says
it is.

The service it reaches is a dictionary. That is deliberate — a vendor, a wire protocol,
or a network call would make a failure ambiguous between "the mechanism is not general"
and "the upstream was down", and the mechanism claim is the only thing under test here.
Every service-specific fact this extension knows (its tool names, its argument shapes,
its behaviour) lives in this file, outside every Arc package, which is exactly the
property the architecture test asserts from the other direction.

``build_native_attachment`` is the fixed, well-known factory name
``arcagent.extension.native_attachment`` asks every direct-implementation extension for.
The name is generic Arc convention; nothing in Arc names *this* extension.
"""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import (
    ProbeResult,
    Requirement,
    RequirementKind,
    ToolOutcome,
    ToolResult,
    ToolSpec,
)

#: The two verbs this extension offers. One read, one write — so the conformance test can
#: assert classification survives translation in both directions, and can prove a
#: round trip through the service rather than through an object it happens to hold.
ECHO_TOOL = "reference_echo"
STORE_TOOL = "reference_store"

#: The credential the operator would supply for a real service. Declared, never read —
#: the fixture must never need a secret to prove the mechanism works.
CREDENTIAL_NAME = "reference_token"


class ReferenceAttachment:
    """An in-memory fake service, reached through the hook and through nothing else.

    The service holds state, and its read verb reads that state. That is what lets a
    caller prove a dispatch landed in the extension's own code without assuming *which*
    instance served it: write through one verb, read it back through the other. Install
    and agent startup each build their own instance — in production they are different
    processes — so any assertion keyed to instance identity would be unsatisfiable.
    """

    def __init__(self, context: dict[str, Any] | None = None) -> None:
        self.context = dict(context or {})
        self.stored: dict[str, str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def requirements(self) -> list[Requirement]:
        """One credential, declared so the install path has something to prompt for."""
        return [
            Requirement(
                kind=RequirementKind.CREDENTIAL,
                name=CREDENTIAL_NAME,
                instruction="Any value; the reference service never reads it.",
            )
        ]

    async def probe(self) -> ProbeResult:
        """Always reachable: the service is this object."""
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail="in-memory reference service",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        """The live tool list, carrying the fields the trifecta gate reads."""
        return [
            ToolSpec(
                name=ECHO_TOOL,
                description=(
                    "Echo back what the reference service holds under this key, "
                    "or the message itself when it holds nothing under it."
                ),
                input_schema={
                    "type": "object",
                    "properties": {"message": {"type": "string"}},
                    "additionalProperties": False,
                },
                classification="read_only",
            ),
            ToolSpec(
                name=STORE_TOOL,
                description="Store a value in the reference service.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "key": {"type": "string"},
                        "value": {"type": "string"},
                    },
                    "additionalProperties": False,
                },
                classification="state_modifying",
                capability_tags=["network_egress"],
            ),
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        """Execute one call. Records it so a caller holding this instance can see it."""
        self.calls.append((tool, dict(args)))
        if tool == ECHO_TOOL:
            message = str(args.get("message", ""))
            return ToolResult(
                tool=tool, content=f"reference echo: {self.stored.get(message, message)}"
            )
        if tool == STORE_TOOL:
            key = str(args.get("key", ""))
            value = str(args.get("value", ""))
            self.stored[key] = value
            return ToolResult(tool=tool, content=f"reference stored {key}")
        return ToolResult(
            tool=tool,
            outcome=ToolOutcome.ERROR,
            content=f"reference service has no tool named {tool!r}",
        )


def build_native_attachment(context: dict[str, Any]) -> ReferenceAttachment:
    """The fixed factory Arc calls to build this extension's attachment."""
    return ReferenceAttachment(context)
