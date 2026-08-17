"""An `@handle` naming nobody must be reportable, not silently dropped.

SPEC-068 F4. ``apply_mentions`` skips a handle that resolves to no registered
entity, which is right for the envelope — an unknown name is prose, not an
address — but it means a targeted message silently becomes an un-addressed
broadcast. A human who typed ``@sales_agent`` when the registry holds ``sales``
gets no mention fanout, no ``action_required``, no priority bump, and no hint
that any of that happened.

``unresolved_mentions`` is the pure query a surface with a person on the other
end uses to refuse the post and name the handle that was wrong.
"""

from __future__ import annotations

from arcteam.mentions import apply_mentions, unresolved_mentions
from arcteam.types import Entity, EntityType, Message

ENTITIES = [
    Entity(
        did="did:arc:local:agent/sales",
        handle="sales",
        id="agent://sales",
        name="Sales",
        type=EntityType.AGENT,
    ),
    Entity(
        did="did:arc:local:agent/ops",
        handle="ops",
        id="agent://ops",
        name="Ops",
        type=EntityType.AGENT,
    ),
]


def test_unknown_handle_is_reported() -> None:
    assert unresolved_mentions(ENTITIES, "ping @sales_agent about this") == ["sales_agent"]


def test_known_handles_are_not_reported() -> None:
    assert unresolved_mentions(ENTITIES, "@sales and @ops please look") == []


def test_reports_every_unknown_once_in_order() -> None:
    body = "@ghost @sales @phantom @ghost"
    assert unresolved_mentions(ENTITIES, body) == ["ghost", "phantom"]


def test_no_mentions_is_empty() -> None:
    assert unresolved_mentions(ENTITIES, "no addresses here") == []


def test_agrees_with_apply_mentions() -> None:
    """The two views of the same body must not disagree about what resolved.

    ``apply_mentions`` records what DID resolve; ``unresolved_mentions`` records
    what did not. A handle must land in exactly one of them, or a surface could
    refuse a post whose mention actually routed, or pass one that did not.
    """
    body = "@sales @ghost @ops"
    message = Message(sender="agent://sales", to=["channel://x"], body=body)
    apply_mentions(ENTITIES, message)
    assert len(message.mentions) == 2
    assert unresolved_mentions(ENTITIES, body) == ["ghost"]
