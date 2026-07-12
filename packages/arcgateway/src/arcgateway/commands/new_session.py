"""``/new`` — start a fresh session with the current agent.

Rotation is a session-key change, not a file wipe: ``router.new_session``
bumps the (agent, user) generation so the next message hashes to a brand-new,
empty session log. The prior conversation stays on disk, resumable if the
generation is ever rolled back.
"""

from __future__ import annotations

from arcgateway.commands.base import CommandContext


class NewSessionCommand:
    """Rotate the caller's session so their next message starts fresh."""

    name = "new"
    aliases: tuple[str, ...] = ("reset",)
    description = "Start a fresh session (clears this conversation's context)."
    required_role: str | None = None

    async def handle(self, ctx: CommandContext) -> str | None:
        ctx.router.new_session(ctx.agent_did, ctx.user_did)
        return "Started a fresh session — this conversation's context has been reset."
