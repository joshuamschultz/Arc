"""T-1191 (SPEC-083 COMP-014) — the candidate renderer.

RED intent: ``arcmemory.promotion.render`` does not exist yet, so the import fails
with ``ModuleNotFoundError`` — the feature is absent.

Contract under test (SDD COMP-014):

- ``render_candidate(item: Insight | Procedure | Entity) -> PromotionText`` with
  fields ``item_kind, item_id, title, content, content_sha256, classification``.
- The body is the one ``arcagent/modules/memory/promotion.py::_describe`` produced
  (moved here), so the bytes classified are the bytes published:
    * insight   -> title = content = ``statement``; id = ``item.id``
    * procedure -> content = ``when_to_use`` + blank line + ``- <step>`` lines
                   (empty parts dropped; falls back to ``title``); id = ``slug``
    * entity    -> content = ``- <predicate>: <value>`` lines (falls back to
                   ``name``); title = ``name``; id = ``slug``
- ``content_sha256 == "sha256:" + sha256(content.strip().encode()).hexdigest()`` —
  byte-for-byte the digest ``FleetSharedKnowledgeService._validate_promotion``
  recomputes on the shared side. Anything else and the shared store refuses.
- ``episodic``/``daily`` records are not promotable: rendering one is refused.
"""

from __future__ import annotations

import hashlib
from types import SimpleNamespace

import pytest

from arcmemory.promotion.render import PromotionText, render_candidate
from arcmemory.types import DaySummary, Entity, Event, Fact, Insight, Procedure, Step


def _shared_digest(content: str) -> str:
    """The exact formula ``FleetSharedKnowledgeService._validate_promotion`` uses."""
    return "sha256:" + hashlib.sha256(content.strip().encode()).hexdigest()


def _insight(statement: str = "Acme renewal closes at $42k/yr, net-60.") -> Insight:
    return Insight(
        id="acme-renewal",
        statement=statement,
        trigger="a renewal negotiation with a named client",
        classification="cui",
    )


def _procedure(**overrides: object) -> Procedure:
    fields: dict[str, object] = {
        "slug": "close-monthly-books",
        "title": "Close the monthly books",
        "when_to_use": "At month end, before the board pack is sent.",
        "steps": [Step(text="Reconcile the bank feeds"), Step(text="Post accruals")],
    }
    fields.update(overrides)
    return Procedure(**fields)


def _entity(**overrides: object) -> Entity:
    fields: dict[str, object] = {
        "slug": "acme-corp",
        "name": "Acme Corp",
        "facts": [
            Fact(predicate="renewal_price", value="$42k/yr"),
            Fact(predicate="payment_terms", value="net-60"),
        ],
    }
    fields.update(overrides)
    return Entity(**fields)


def test_render_insight_uses_statement_as_title_and_body() -> None:
    text = render_candidate(_insight())

    assert isinstance(text, PromotionText)
    assert text.item_kind == "insight"
    assert text.item_id == "acme-renewal"
    assert text.title == "Acme renewal closes at $42k/yr, net-60."
    assert text.content == "Acme renewal closes at $42k/yr, net-60."
    assert text.classification == "cui"


def test_render_insight_digest_matches_shared_service_formula() -> None:
    text = render_candidate(_insight())

    assert text.content_sha256 == _shared_digest(text.content)


def test_render_insight_digest_is_over_stripped_content() -> None:
    """Surrounding whitespace never changes the digest the shared side recomputes."""
    padded = render_candidate(_insight("  Acme renewal closes at $42k/yr, net-60.\n\n"))
    tight = render_candidate(_insight("Acme renewal closes at $42k/yr, net-60."))

    assert padded.content_sha256 == _shared_digest(padded.content)
    assert padded.content_sha256 == tight.content_sha256


def test_render_procedure_joins_trigger_and_steps() -> None:
    text = render_candidate(_procedure())

    assert text.item_kind == "procedure"
    assert text.item_id == "close-monthly-books"
    assert text.title == "Close the monthly books"
    assert text.content == (
        "At month end, before the board pack is sent.\n\n"
        "- Reconcile the bank feeds\n- Post accruals"
    )
    assert text.content_sha256 == _shared_digest(text.content)


def test_render_procedure_without_trigger_is_steps_only() -> None:
    text = render_candidate(_procedure(when_to_use=""))

    assert text.content == "- Reconcile the bank feeds\n- Post accruals"


def test_render_procedure_empty_falls_back_to_title() -> None:
    """Content is never empty — the shared service refuses empty content."""
    text = render_candidate(_procedure(when_to_use="", steps=[]))

    assert text.content == "Close the monthly books"
    assert text.content_sha256 == _shared_digest("Close the monthly books")


def test_render_entity_lists_facts() -> None:
    text = render_candidate(_entity())

    assert text.item_kind == "entity"
    assert text.item_id == "acme-corp"
    assert text.title == "Acme Corp"
    assert text.content == "- renewal_price: $42k/yr\n- payment_terms: net-60"
    assert text.content_sha256 == _shared_digest(text.content)
    assert text.classification == "unclassified"


def test_render_entity_without_facts_falls_back_to_name() -> None:
    text = render_candidate(_entity(facts=[]))

    assert text.content == "Acme Corp"


def test_render_is_deterministic_and_content_sensitive() -> None:
    """Same bytes -> same digest (ledger skip); one changed byte -> new digest."""
    first = render_candidate(_insight())
    again = render_candidate(_insight())
    edited = render_candidate(_insight("Acme renewal closes at $43k/yr, net-60."))

    assert first.content_sha256 == again.content_sha256
    assert edited.content_sha256 != first.content_sha256


@pytest.mark.parametrize(
    "item",
    [
        Event(event_id="evt-1", scope="did:arc:a", kind="respond", text="raw episodic turn"),
        DaySummary(day="2026-09-26", decisions=["Ship the renewal"]),
    ],
    ids=["episodic", "daily"],
)
def test_render_refuses_non_promotable_records(item: object) -> None:
    """Episodic events and daily logs are never promotable (REQ-486)."""
    with pytest.raises((TypeError, ValueError)):
        render_candidate(item)  # type: ignore[arg-type]  # asserting the refusal


@pytest.mark.parametrize(
    "item",
    [_insight(), _procedure(), _entity()],
    ids=["insight", "procedure", "entity"],
)
def test_render_output_passes_the_real_shared_service_validation(item: object) -> None:
    """Cross-check against the real shared-side oracle when arcteam is installed.

    Builds the exact source shape ``FleetSharedKnowledgeService.promote`` receives
    and runs the service's own digest validation over the rendered bytes.
    """
    service = pytest.importorskip("arcteam.shared_knowledge.service")
    text = render_candidate(item)  # type: ignore[arg-type]
    source = SimpleNamespace(
        reference=SimpleNamespace(scope="personal", identifier=text.item_id),
        digest=text.content_sha256,
        content=text.content,
        title=text.title,
    )

    service.FleetSharedKnowledgeService._validate_promotion(source)
