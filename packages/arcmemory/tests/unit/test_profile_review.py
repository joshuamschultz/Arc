"""Provenance-bearing profile facts require an operator review decision."""

from __future__ import annotations

from arcmemory.profile import ProfileFactKind, ProfileReviewStore, ReviewPort, ReviewStatus
from arcmemory.types import Provenance


async def test_pending_fact_is_invisible_until_approved_then_undo_restores_prior(
    workspace,
) -> None:
    reviews = ProfileReviewStore(workspace)
    assert isinstance(reviews, ReviewPort)
    prior = await reviews.submit(
        profile_id="olivia",
        field="role",
        value="designer",
        kind=ProfileFactKind.STATIC,
        provenance=Provenance(source="crm", external_id="olivia-v1"),
    )
    await reviews.approve(prior.fact_id)
    replacement = await reviews.submit(
        profile_id="olivia",
        field="role",
        value="product lead",
        kind=ProfileFactKind.INFERRED,
        provenance=Provenance(source="mail", external_id="thread-8"),
    )

    assert (await reviews.context("olivia")).static["role"] == "designer"
    assert (await reviews.list(status=ReviewStatus.PENDING))[0].fact_id == replacement.fact_id
    await reviews.approve(replacement.fact_id)
    assert (await reviews.context("olivia")).inferred["role"] == "product lead"

    await reviews.undo(replacement.fact_id)
    context = await reviews.context("olivia")
    assert context.static["role"] == "designer"
    assert (await reviews.get(replacement.fact_id)).status == ReviewStatus.UNDONE


async def test_declined_and_lower_clearance_facts_never_surface(workspace) -> None:
    reviews = ProfileReviewStore(workspace)
    fact = await reviews.submit(
        profile_id="olivia",
        field="project",
        value="classified launch",
        kind=ProfileFactKind.DYNAMIC,
        provenance=Provenance(source="email", external_id="mail-1", classification="secret"),
        classification="secret",
    )
    await reviews.decline(fact.fact_id)

    assert (await reviews.context("olivia", clearance="top_secret")).dynamic == {}
    assert await reviews.recall("olivia", "launch", clearance="top_secret") == []
