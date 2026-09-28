"""T-1187 (SPEC-083 COMP-012) — the pre-egress secret gate.

RED intent: ``arcmemory.promotion.secret_gate`` does not exist yet, so the import
fails with ``ModuleNotFoundError`` — the feature is absent.

Contract under test (SDD COMP-012, README decisions 3 and 5):

- ``arcmemory.promotion.secret_gate.contains_secret(text: str) -> bool``.
- True when the text matches EITHER the conservative credential keyword regex
  (moved verbatim from ``promotion/filter.py``:
  ``passwd|password|secret|api[_-]?key|apikey|access[_-]?key|private[_-]?key|
  token|credential|bearer``, word-bounded, case-insensitive) OR any value pattern
  in ``arctrust.secrets.SECRET_PATTERNS`` (AWS, GitHub, JWT, PEM, DB URL, ...).
- Email addresses, phone numbers and client/person names are NOT secrets: deal
  text full of them must pass (False). The old PII blockers are deleted.
- Any internal exception -> True (fail closed). An exploding gate must never
  wave an item through to a third-party classifier.

The structured-value cases below are chosen so they contain NONE of the keyword
words, so each one proves the ``SECRET_PATTERNS`` half is actually consulted.
"""

from __future__ import annotations

import re
from typing import cast

import pytest
from arcmemory.promotion.secret_gate import contains_secret
from arctrust.secrets import SECRET_PATTERNS

# --- keyword cases (moved from test_privacy_filter.py + the rest of the regex) ---

_KEYWORD_CASES = [
    "The prod DB password is hunter2-Sup3rSecret and must not leak.",
    "Use the passwd from the shared vault entry.",
    "Keep the secret out of the runbook.",
    "The api_key for the billing sandbox rotates monthly.",
    "Rotate the api-key before the audit.",
    "Paste the apikey into the provider console.",
    "The access_key lives in the ops folder.",
    "Never paste a private_key into chat.",
    "Refresh the token every hour.",
    "The service credential expires Friday.",
    "Send it as a Bearer header.",
    "The PASSWORD for the staging box changed.",
]

# --- structured values from arctrust.secrets.SECRET_PATTERNS (no keyword words) ---

_AWS = "Deploy with AKIAIOSFODNN7EXAMPLE on the east cluster."
_GITHUB = "CI uses ghp_" + "a" * 36 + " for the release job."
_JWT = (
    "Session blob eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
    "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U was captured."
)
_PEM = (
    "Attached below:\n-----BEGIN OPENSSH PRIVATE KEY-----\n"
    "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAABAAAAMwAAAAtzc2gtZW\n"
    "-----END OPENSSH PRIVATE KEY-----\n"
)
_DB_URL = "Reports read from postgres://admin:hunter2@db.internal:5432/prod nightly."

# --- clean company deal text: email, phone, client name -> NOT a secret -------

_DEAL = (
    "Acme Corp renewal closes at $42k/yr, net-60. Primary contact is Jane Doe "
    "(jane.doe@acme.example, 555-123-4567); counterparty legal is Bob Smith."
)
_PROCESS = "The standard process for closing the monthly books."


@pytest.mark.parametrize("text", _KEYWORD_CASES)
def test_contains_secret_credential_keyword_returns_true(text: str) -> None:
    """The mere WORD of a credential is enough to keep the item on the box."""
    assert contains_secret(text) is True


@pytest.mark.parametrize(
    ("label", "text"),
    [
        ("aws_access_key", _AWS),
        ("github_token", _GITHUB),
        ("jwt", _JWT),
        ("pem_block", _PEM),
        ("db_url_with_password", _DB_URL),
    ],
)
def test_contains_secret_structured_secret_value_returns_true(label: str, text: str) -> None:
    """A credential VALUE with no keyword word is still caught via SECRET_PATTERNS."""
    assert contains_secret(text) is True, label


_KEYWORD_RE = re.compile(
    r"\b(passwd|password|secret|api[_-]?key|apikey|access[_-]?key|"
    r"private[_-]?key|token|credential|bearer)\b",
    re.IGNORECASE,
)


@pytest.mark.parametrize("text", [_AWS, _GITHUB, _JWT, _PEM, _DB_URL])
def test_structured_fixture_rides_on_a_pattern_not_a_keyword(text: str) -> None:
    """Guard on the fixtures: each value case matches SECRET_PATTERNS and no keyword.

    If a fixture accidentally contained ``password``/``token`` the structured
    tests would pass on the keyword half alone and prove nothing about the
    ``SECRET_PATTERNS`` half.
    """
    assert _KEYWORD_RE.search(text) is None
    assert any(pattern.search(text) for _, pattern in SECRET_PATTERNS)


def test_contains_secret_reads_the_canonical_arctrust_list() -> None:
    """A pattern added to SECRET_PATTERNS is enforced without touching the gate.

    Proves the gate consults the canonical arctrust list per call rather than a
    private copy that would silently drift from it. The list is mutated in place
    so a ``from arctrust.secrets import SECRET_PATTERNS`` binding sees it too.
    """
    marker = "ZZCANARYVALUE0123456789"
    text = f"Harmless note carrying {marker} inside."
    assert contains_secret(text) is False  # narrowness: not a secret before

    SECRET_PATTERNS.append(("CANARY", re.compile(marker)))
    try:
        assert contains_secret(text) is True
    finally:
        SECRET_PATTERNS.pop()


@pytest.mark.parametrize("text", [_DEAL, _PROCESS])
def test_contains_secret_clean_company_text_with_pii_returns_false(text: str) -> None:
    """Email, phone and client names are company knowledge, not secrets (decision 3)."""
    assert contains_secret(text) is False


def test_contains_secret_empty_text_returns_false() -> None:
    """Nothing to leak in an empty string; the gate must not crash on it."""
    assert contains_secret("") is False


class _ExplodingPattern:
    """A stand-in compiled pattern whose every matching method raises."""

    pattern = "boom"

    def _boom(self, *_: object, **__: object) -> object:
        raise RuntimeError("regex engine exploded")

    search = match = fullmatch = finditer = findall = _boom


def test_contains_secret_internal_exception_fails_closed() -> None:
    """A pattern that raises mid-scan makes the gate answer True, never False.

    The canonical list is patched IN PLACE, so the exploding pattern is seen
    whether the gate iterates ``arctrust.secrets.SECRET_PATTERNS`` or holds a
    ``from ... import`` binding to the same list object. ``_DEAL`` is clean, so
    only the fail-closed branch can produce True here.
    """
    original = SECRET_PATTERNS[0]
    SECRET_PATTERNS[0] = ("BOOM", _ExplodingPattern())  # type: ignore[assignment]  # fault injection
    try:
        assert contains_secret(_DEAL) is True
    finally:
        SECRET_PATTERNS[0] = original


def test_contains_secret_non_string_input_fails_closed() -> None:
    """A malformed (non-str) input is an internal error: treat it as a secret."""
    assert contains_secret(cast(str, object())) is True
