"""A native OAuth connector whose "service" is this object — the fixture the
``complete_oauth`` flow is tested against. Its probe reports whether the refresh
token the flow stores actually reached the rebuilt attachment, so a test can prove
the exchange's result was persisted and delivered, not merely returned.
"""

from __future__ import annotations

from typing import Any

from arcagent.extension.attachment import ProbeResult, Requirement, ToolResult, ToolSpec


class OAuthReferenceAttachment:
    """Reachable iff it was built holding a refresh token (what connect must store)."""

    def __init__(self, context: dict[str, Any] | None = None) -> None:
        self.refresh_token = str((context or {}).get("refresh_token", ""))

    def requirements(self) -> list[Requirement]:
        return []

    async def probe(self) -> ProbeResult:
        held = "authenticated" if self.refresh_token else "unauthenticated"
        return ProbeResult(
            reachable=True,
            tools=await self.describe_tools(),
            detail=f"oauth reference service ({held})",
        )

    async def describe_tools(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                name="oauth_ping",
                description="Report whether the refresh token arrived.",
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                classification="read_only",
            )
        ]

    async def invoke(self, tool: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(tool=tool, content="authenticated" if self.refresh_token else "no token")


def build_native_attachment(context: dict[str, Any]) -> OAuthReferenceAttachment:
    return OAuthReferenceAttachment(context)
