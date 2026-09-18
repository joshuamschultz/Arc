"""T-1118 (SPEC-083 COMP-003) — the hard privacy filter (raise-only).

RED intent: ``arcmemory.promotion`` does not exist yet, so importing the filter
fails with ``ModuleNotFoundError`` — the feature is absent.

Intended contract (for the GREEN implementer):

- ``arcmemory.promotion.filter.privacy_floor(item, *, other_person_check=None) -> int``
  returns the *minimum* effective score an item may carry. ``item`` is the item's
  text content (``str``) in v1. Rules (all raise the floor to >=8):
    * deterministic: secret/credential patterns, emails, phone numbers;
    * injected LLM check: ``other_person_check(item) -> bool`` — True means the
      text references another person / another user / another session;
    * any exception inside ``other_person_check`` -> floor 8 (fail-closed).
  A clean item gets a low floor (1).
- ``arcmemory.promotion.filter.effective_score(personal_score, item, *,
  other_person_check=None) -> int`` == ``max(personal_score or 8, privacy_floor(...))``.
  It NEVER lowers a score; an unscored item (``None``) is treated as 8.

The deterministic rules are real; the "other person/session" check is injected so
the test is deterministic and never calls a real model.
"""

from __future__ import annotations

from arcmemory.promotion.filter import effective_score, privacy_floor

_CLEAN = "The standard process for closing the monthly books."
_SECRET = "The prod DB password is hunter2-Sup3rSecret and must not leak."
_EMAIL = "Reach the vendor rep at jane.doe@example.com about the renewal."
_PHONE = "Call the field office at 555-123-4567 to confirm the shipment."


def _raises(_: str) -> bool:
    raise RuntimeError("classifier exploded")


def test_clean_item_gets_low_floor() -> None:
    """Ordinary company text imposes no privacy floor to speak of."""
    assert privacy_floor(_CLEAN) == 1


def test_secret_pattern_forces_floor_at_least_eight() -> None:
    """A credential/secret in the text floors the item into the never band."""
    assert privacy_floor(_SECRET) >= 8


def test_email_forces_floor_at_least_eight() -> None:
    """An email address is PII — floored into the never band."""
    assert privacy_floor(_EMAIL) >= 8


def test_phone_forces_floor_at_least_eight() -> None:
    """A phone number is PII — floored into the never band."""
    assert privacy_floor(_PHONE) >= 8


def test_other_person_check_forces_floor_at_least_eight() -> None:
    """The injected 'another person/session' check raises the floor."""
    floor = privacy_floor(_CLEAN, other_person_check=lambda _: True)

    assert floor >= 8


def test_other_person_check_error_is_fail_closed() -> None:
    """An exception inside the injected check floors to 8, not 1."""
    floor = privacy_floor(_CLEAN, other_person_check=_raises)

    assert floor == 8


def test_effective_score_never_lowers_a_clean_score() -> None:
    """A clean item does not raise (or lower) an already-low score."""
    assert effective_score(2, _CLEAN) == 2


def test_effective_score_raises_a_low_score_for_pii() -> None:
    """A low score on a PII-bearing item is raised into the never band."""
    assert effective_score(2, _SECRET) >= 8


def test_effective_score_unscored_is_treated_as_eight() -> None:
    """An unscored (``None``) item is treated as 8 even when clean — fail-closed."""
    assert effective_score(None, _CLEAN) >= 8


def test_effective_score_keeps_a_high_score_on_a_clean_item() -> None:
    """A high personal score is preserved when the item itself is clean."""
    assert effective_score(6, _CLEAN) == 6
