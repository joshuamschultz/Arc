"""The Notes connector: one read-only tool over an in-memory service.

The token arrives through the credential handle at call time, the way a real native
attachment reads a sensitive field. The tool answers whether a token arrived, never
the token itself.
"""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, Requirement, ToolResult, ToolSpec

ECHO_TOOL = "notes_echo"
TOKEN_FIELD = "api_token"


class NotesAttachment:
    """Echoes a note back, after checking that a token was supplied."""

    def __init__(self, context: dict[str, Any]) -> None:
        self._handle = context.get("credential")

    async def _has_token(self) -> bool:
        if self._handle is None:
            return False
        found = await self._handle.maybe_field(TOKEN_FIELD)
        return found is not None and bool(found.reveal())

    def requirements(self) -> list[Requirement]:
        return []

    async def probe(self) -> ProbeResult:
        held = await self._has_token()
        return ProbeResult(
            reachable=held,
            tools=await self.describe_tools(),
            detail="notes service" if held else "no token",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name=ECHO_TOOL,
                description="Echo a note back from the Notes service.",
                input_schema={
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "additionalProperties": False,
                },
                classification="read_only",
            )
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        token = "token ok" if await self._has_token() else "no token"
        return ToolResult(tool=tool, content=f"note: {args.get('text', '')} ({token})")


def build_native_attachment(context: dict[str, Any]) -> NotesAttachment:
    """The fixed factory Arc calls to build this connector's attachment."""
    return NotesAttachment(context)
