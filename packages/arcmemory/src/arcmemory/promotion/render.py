"""Candidate renderer — the one body text for a promotable item (SPEC-083 COMP-014).

The bytes classified are byte-for-byte the bytes published: the sweep, the
ledger and the exporter all render through :func:`render_candidate`, and the
digest is the same formula ``FleetSharedKnowledgeService._validate_promotion``
recomputes on the shared side (``sha256`` of ``content.strip()``).

Only insights, procedures and entities are promotable. Episodic events and daily
logs are refused (REQ-486).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Literal

from arcmemory.types import Entity, Insight, Procedure

PromotableKind = Literal["insight", "procedure", "entity"]
PROMOTABLE_KINDS: tuple[PromotableKind, ...] = ("insight", "procedure", "entity")


def require_card_id(item_id: str) -> None:
    """Refuse an id that could name anything but one card in its store (path jail)."""
    if item_id in ("", ".", "..") or any(sep in item_id for sep in ("/", "\\", "\0")):
        raise ValueError("memory reference must name exactly one card")


@dataclass(frozen=True)
class PromotionText:
    """The rendered, hashed form of one promotable memory item."""

    item_kind: PromotableKind
    item_id: str
    title: str
    content: str
    content_sha256: str
    classification: str


def content_digest(content: str) -> str:
    """The shared store's digest: ``"sha256:"`` + hex of the stripped UTF-8 bytes."""
    return "sha256:" + hashlib.sha256(content.strip().encode("utf-8")).hexdigest()


def render_candidate(item: Insight | Procedure | Entity) -> PromotionText:
    """Render ``item`` to its promotable body. Content is never empty.

    Raises ``TypeError`` for any record that is not an insight, procedure or entity.
    """
    if isinstance(item, Insight):
        return _text("insight", item.id, item.statement, item.statement, item.classification)
    if isinstance(item, Procedure):
        steps = "\n".join(f"- {step.text}" for step in item.steps)
        content = "\n\n".join(part for part in (item.when_to_use, steps) if part)
        return _text(
            "procedure", item.slug, item.title, content or item.title, item.classification
        )
    if isinstance(item, Entity):
        facts = "\n".join(f"- {fact.predicate}: {fact.value}" for fact in item.facts)
        return _text("entity", item.slug, item.name, facts or item.name, item.classification)
    raise TypeError(f"{type(item).__name__} is not a promotable memory record")


def _text(
    kind: PromotableKind, item_id: str, title: str, content: str, classification: str
) -> PromotionText:
    return PromotionText(
        item_kind=kind,
        item_id=item_id,
        title=title,
        content=content,
        content_sha256=content_digest(content),
        classification=classification,
    )


__all__ = [
    "PROMOTABLE_KINDS",
    "PromotableKind",
    "PromotionText",
    "content_digest",
    "render_candidate",
    "require_card_id",
]
