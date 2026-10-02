"""``/gate`` — resolve a waiting workflow gate from chat (J3 F8).

A run that reaches a ``gate`` node stops until a human decides. Before this the
only place to decide was the dashboard, so a Telegram or Slack user got a
narration line and no way to act on it. ``/gate <task_id> approve|reject|revise
[notes]`` is that card: it calls the control plane's one ``resolve_gate`` with the
paired human's DID, exactly as the dashboard route and ``arc workflow gate`` do.

The command holds no decision logic. Words map to decisions in the control plane,
and what each decision does to the run is the runner's business.
"""

from __future__ import annotations

from arcgateway.commands.base import CommandContext, GateResolver

DECISION_WORDS: tuple[str, ...] = ("approve", "reject", "revise")

_USAGE = (
    "Resolve a waiting workflow gate:\n"
    "/gate <task_id> approve [notes]  - let the run continue\n"
    "/gate <task_id> reject [notes]   - fail the run\n"
    "/gate <task_id> revise [notes]   - send the work back for another pass"
)


class GateCommand:
    """Approve, reject, or send back a workflow gate, as the paired user."""

    name = "gate"
    aliases: tuple[str, ...] = ()
    description = "Approve, reject, or send back a waiting workflow gate."
    required_role: str | None = None

    def __init__(self, resolver: GateResolver) -> None:
        self._resolver = resolver

    async def handle(self, ctx: CommandContext) -> str | None:
        """Parse ``<task_id> <word> [notes]`` and resolve the gate for ``ctx.user_did``."""
        parts = ctx.args.split(maxsplit=2)
        if len(parts) < 2:
            return _USAGE
        task_id, word = parts[0], parts[1].lower()
        notes = parts[2] if len(parts) > 2 else ""
        if word not in DECISION_WORDS:
            return f"I don't know {word!r}. Use approve, reject, or revise.\n\n{_USAGE}"
        outcome = await self._resolver.resolve_gate(
            task_id, decision=word, notes=notes, actor_did=ctx.user_did
        )
        return outcome
