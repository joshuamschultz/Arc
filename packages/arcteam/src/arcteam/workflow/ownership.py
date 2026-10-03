"""The template owner placeholder, and the guard that refuses to run on it.

Starter templates ship ``owner = "@operator"`` because a template cannot know
which agent a deployment has. No agent answers to it, so a node that falls back
to the workflow owner would wait on nobody. Signing and run start refuse such a
workflow up front, naming the nodes and the fix.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from .errors import PlaceholderOwnerError

PLACEHOLDER_OWNER = "@operator"


def unowned_node_ids(owner: str | None, nodes: Sequence[Any]) -> tuple[str, ...]:
    """Ids of nodes that fall back to a placeholder workflow owner.

    A gate is decided by a human and a node naming its own agent never reads
    the workflow owner, so neither is blocked by the placeholder.
    """
    if owner != PLACEHOLDER_OWNER:
        return ()
    return tuple(n.id for n in nodes if n.kind != "gate" and not n.agent)


def assert_owner_is_real(workflow_id: str, owner: str | None, nodes: Sequence[Any]) -> None:
    """Raise :class:`PlaceholderOwnerError` when a node would wait on the placeholder."""
    blocked = unowned_node_ids(owner, nodes)
    if blocked:
        raise PlaceholderOwnerError(workflow_id, owner or PLACEHOLDER_OWNER, blocked)
