"""Facts must gain confidence when corroborated, and lose CURRENCY as they age.

Two separate defects met in one place on a live agent. Every entity fact written
through the agentic tool path sat at exactly 0.5, because that path took whatever
confidence the model supplied and defaulted to 0.5 — while the deterministic
distiller path corroborated properly. And ``merge_facts`` resolves a conflict by
"higher confidence wins, ties go to the incumbent", so at a permanent 0.5 a
CORRECTED fact could never replace the one it corrected. Someone changes jobs and
the old employer wins forever.

Ageing is handled as a view, never a mutation: a fact that has not been restated
in months is less likely to be CURRENT, but it is still what was true then, and
the operator was explicit that old facts remain history. Stored confidence is the
evidence and never decays; currency is evidence discounted by age, and only that
is used to decide which value leads.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from arcmemory.config import MemoryConfig
from arcmemory.stores.semantic import corroborate, current_confidence, merge_facts
from arcmemory.types import Fact

_CFG = MemoryConfig()
_NOW = datetime(2026, 8, 15, tzinfo=UTC)


def _fact(value: str, *, confidence: float, days_old: int = 0) -> Fact:
    return Fact(
        predicate="employer",
        value=value,
        confidence=confidence,
        date=(_NOW - timedelta(days=days_old)).strftime("%Y-%m-%d"),
    )


def test_restating_a_fact_raises_its_confidence(store_gamma: float = _CFG.gamma) -> None:
    """Seeing the same thing twice is more evidence than seeing it once."""
    once = corroborate(None, 0.0, gamma=store_gamma)
    twice = corroborate(once, once, gamma=store_gamma)

    assert 0.4 < once < 0.5, "one sighting must not read as established"
    assert twice > once
    assert corroborate(twice, twice, gamma=store_gamma) > twice


def test_corroboration_saturates_rather_than_reaching_certainty() -> None:
    """Memory never claims certainty — the curve approaches 1 and never arrives."""
    confidence = 0.0
    for _ in range(20):
        confidence = corroborate(confidence, confidence, gamma=_CFG.gamma)

    assert 0.99 < confidence < 1.0


def test_stored_confidence_never_decays() -> None:
    """The evidence is history and stays whole; only its CURRENCY is discounted."""
    old = _fact("Acme", confidence=0.8, days_old=400)

    assert old.confidence == 0.8, "the record of what was believed was rewritten"
    assert current_confidence(old, now=_NOW, half_life_days=_CFG.fact_half_life_days) < 0.8


def test_a_fresh_fact_outranks_an_equally_evidenced_stale_one() -> None:
    """The tie that made corrections impossible: 0.5 is never greater than 0.5."""
    stale = _fact("Acme", confidence=0.5, days_old=365)
    fresh = _fact("Globex", confidence=0.5, days_old=0)

    assert current_confidence(fresh, now=_NOW, half_life_days=_CFG.fact_half_life_days) > (
        current_confidence(stale, now=_NOW, half_life_days=_CFG.fact_half_life_days)
    )


def test_a_correction_supersedes_the_fact_it_corrects(monkeypatch: object) -> None:
    """End to end: today's employer leads, and yesterday's is kept as the trail."""
    stale = _fact("Acme", confidence=0.5, days_old=365)
    fresh = _fact("Globex", confidence=0.5, days_old=0)

    merged = merge_facts(stale, fresh, now=_NOW, half_life_days=_CFG.fact_half_life_days)

    assert merged.value == "Globex", "the correction lost to the fact it corrected"
    assert merged.was_value == "Acme", "the superseded value was discarded instead of kept"


def test_a_well_evidenced_fact_is_not_unseated_by_a_single_fresh_mention() -> None:
    """Recency must tilt the scale, not own it, or one offhand remark rewrites memory."""
    established = _fact("Acme", confidence=0.95, days_old=60)
    offhand = _fact("Globex", confidence=0.41, days_old=0)

    merged = merge_facts(established, offhand, now=_NOW, half_life_days=_CFG.fact_half_life_days)

    assert merged.value == "Acme"
    assert merged.was_value == "Globex"


def test_restating_the_same_value_corroborates_instead_of_competing() -> None:
    """Agreement is evidence: two sightings of one value must raise it, not tie."""
    first = _fact("Acme", confidence=0.41, days_old=30)
    again = _fact("Acme", confidence=0.41, days_old=0)

    merged = merge_facts(first, again, now=_NOW, half_life_days=_CFG.fact_half_life_days)

    assert merged.value == "Acme"
    assert merged.confidence > 0.41, "a second sighting added no evidence"
    assert merged.was_value is None, "agreement is not a contradiction trail"
