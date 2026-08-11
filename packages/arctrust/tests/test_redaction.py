"""Neutral redaction and secret-detection contract tests."""

import re

import pytest

import arctrust


@pytest.mark.parametrize(
    ("text", "pii_type", "namespace"),
    [
        ("SSN 123-45-6789", "SSN", "PII"),
        ("mail person@example.com", "EMAIL", "PII"),
        ("card 4111111111111111", "CREDIT_CARD", "PII"),
        ("key AKIAIOSFODNN7EXAMPLE", "AWS_ACCESS_KEY", "SECRET"),
        (f"token ghp_{'a' * 36}", "GITHUB_TOKEN", "SECRET"),
    ],
)
def test_canonical_detector_and_redactor(text: str, pii_type: str, namespace: str) -> None:
    matches = arctrust.RegexPiiDetector().detect(text)
    match = next(item for item in matches if item.pii_type == pii_type)

    assert match.namespace == namespace
    assert match.matched_text not in arctrust.redact_text(text, matches)
    assert f"[{namespace}:{pii_type}]" in arctrust.redact_text(text, matches)


def test_checksum_shaped_noise_is_not_redacted() -> None:
    detector = arctrust.RegexPiiDetector()

    assert not any(
        match.pii_type == "CREDIT_CARD" for match in detector.detect("order 1234567890123456")
    )


def test_scan_length_bounds_adversarial_suffix() -> None:
    text = "x" * arctrust.MAX_REGEX_SCAN_LENGTH + " 123-45-6789"

    assert arctrust.RegexPiiDetector().detect(text) == []


def test_overlapping_matches_are_stable_and_non_overlapping() -> None:
    detector = arctrust.RegexPiiDetector(
        custom_patterns=[
            {"name": "SHORT", "pattern": r"abc"},
            {"name": "LONG", "pattern": r"abcdef"},
        ],
        entities={"allow": ["SHORT", "LONG"]},
    )

    assert [(match.pii_type, match.matched_text) for match in detector.detect("abcdef")] == [
        ("LONG", "abcdef")
    ]


def test_invalid_custom_regex_fails_closed() -> None:
    with pytest.raises(arctrust.RedactionConfigError, match="Invalid regex"):
        arctrust.RegexPiiDetector(custom_patterns=[{"name": "BAD", "pattern": "("}])


def test_secret_patterns_are_compiled_and_bounded() -> None:
    patterns = dict(arctrust.SECRET_PATTERNS)

    assert all(isinstance(pattern, re.Pattern) for pattern in patterns.values())
    assert "{1,20000}?" in patterns["PEM_BLOCK"].pattern
