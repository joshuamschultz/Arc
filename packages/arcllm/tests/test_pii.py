"""Tests for PII detection and redaction."""

import pytest

from arcllm._pii import PiiMatch, RegexPiiDetector, redact_text
from arcllm.exceptions import ArcLLMConfigError


# ---------------------------------------------------------------------------
# PiiMatch dataclass
# ---------------------------------------------------------------------------


class TestPiiMatch:
    def test_pii_match_fields(self):
        match = PiiMatch(pii_type="SSN", start=0, end=11, matched_text="123-45-6789")
        assert match.pii_type == "SSN"
        assert match.start == 0
        assert match.end == 11
        assert match.matched_text == "123-45-6789"

    def test_pii_match_equality(self):
        a = PiiMatch(pii_type="SSN", start=0, end=11, matched_text="123-45-6789")
        b = PiiMatch(pii_type="SSN", start=0, end=11, matched_text="123-45-6789")
        assert a == b


# ---------------------------------------------------------------------------
# RegexPiiDetector — SSN
# ---------------------------------------------------------------------------


class TestDetectSSN:
    def test_detects_ssn(self):
        detector = RegexPiiDetector()
        matches = detector.detect("My SSN is 123-45-6789 thanks")
        assert len(matches) == 1
        assert matches[0].pii_type == "SSN"
        assert matches[0].matched_text == "123-45-6789"

    def test_detects_multiple_ssns(self):
        detector = RegexPiiDetector()
        matches = detector.detect("SSN: 123-45-6789 and 987-65-4321")
        ssn_matches = [m for m in matches if m.pii_type == "SSN"]
        assert len(ssn_matches) == 2

    def test_no_false_positive_partial_ssn(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Number 123-45-67890 is not an SSN")
        ssn_matches = [m for m in matches if m.pii_type == "SSN"]
        assert len(ssn_matches) == 0


# ---------------------------------------------------------------------------
# RegexPiiDetector — Credit Card
# ---------------------------------------------------------------------------


class TestDetectCreditCard:
    def test_detects_credit_card_spaces(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Card: 4111 1111 1111 1111")
        cc_matches = [m for m in matches if m.pii_type == "CREDIT_CARD"]
        assert len(cc_matches) == 1

    def test_detects_credit_card_dashes(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Card: 4111-1111-1111-1111")
        cc_matches = [m for m in matches if m.pii_type == "CREDIT_CARD"]
        assert len(cc_matches) == 1

    def test_detects_credit_card_no_separators(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Card: 4111111111111111")
        cc_matches = [m for m in matches if m.pii_type == "CREDIT_CARD"]
        assert len(cc_matches) == 1


# ---------------------------------------------------------------------------
# RegexPiiDetector — Email
# ---------------------------------------------------------------------------


class TestDetectEmail:
    def test_detects_email(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Email me at user@example.com please")
        email_matches = [m for m in matches if m.pii_type == "EMAIL"]
        assert len(email_matches) == 1
        assert email_matches[0].matched_text == "user@example.com"

    def test_detects_complex_email(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Contact: first.last+tag@sub.domain.org")
        email_matches = [m for m in matches if m.pii_type == "EMAIL"]
        assert len(email_matches) == 1

    def test_no_false_positive_at_sign(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Use @mentions in chat")
        email_matches = [m for m in matches if m.pii_type == "EMAIL"]
        assert len(email_matches) == 0


# ---------------------------------------------------------------------------
# RegexPiiDetector — Phone
# ---------------------------------------------------------------------------


class TestDetectPhone:
    def test_detects_us_phone(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Call 555-123-4567")
        phone_matches = [m for m in matches if m.pii_type == "PHONE"]
        assert len(phone_matches) == 1

    def test_detects_phone_with_parens(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Call (555) 123-4567")
        phone_matches = [m for m in matches if m.pii_type == "PHONE"]
        assert len(phone_matches) == 1

    def test_detects_phone_with_country_code(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Call +1-555-123-4567")
        phone_matches = [m for m in matches if m.pii_type == "PHONE"]
        assert len(phone_matches) == 1


# ---------------------------------------------------------------------------
# RegexPiiDetector — IPv4
# ---------------------------------------------------------------------------


class TestDetectIPv4:
    def test_detects_ipv4(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Server at 192.168.1.100")
        ip_matches = [m for m in matches if m.pii_type == "IPV4"]
        assert len(ip_matches) == 1
        assert ip_matches[0].matched_text == "192.168.1.100"

    def test_no_false_positive_version_number(self):
        """Version numbers like 1.2.3 should not match (only 3 octets)."""
        detector = RegexPiiDetector()
        matches = detector.detect("Version 1.2.3 released")
        ip_matches = [m for m in matches if m.pii_type == "IPV4"]
        assert len(ip_matches) == 0


# ---------------------------------------------------------------------------
# No false positives
# ---------------------------------------------------------------------------


class TestNoFalsePositives:
    def test_clean_text(self):
        detector = RegexPiiDetector()
        matches = detector.detect("This is a perfectly clean message with no PII.")
        assert len(matches) == 0

    def test_empty_string(self):
        detector = RegexPiiDetector()
        matches = detector.detect("")
        assert len(matches) == 0


# ---------------------------------------------------------------------------
# Custom patterns
# ---------------------------------------------------------------------------


class TestCustomPatterns:
    def test_custom_pattern(self):
        detector = RegexPiiDetector(
            custom_patterns=[{"name": "EMPLOYEE_ID", "pattern": r"EMP-\d{6}"}]
        )
        matches = detector.detect("Employee EMP-123456 reported")
        custom_matches = [m for m in matches if m.pii_type == "EMPLOYEE_ID"]
        assert len(custom_matches) == 1
        assert custom_matches[0].matched_text == "EMP-123456"

    def test_custom_pattern_alongside_builtin(self):
        detector = RegexPiiDetector(
            custom_patterns=[{"name": "EMPLOYEE_ID", "pattern": r"EMP-\d{6}"}]
        )
        matches = detector.detect("EMP-123456 email: user@test.com")
        types = {m.pii_type for m in matches}
        assert "EMPLOYEE_ID" in types
        assert "EMAIL" in types

    def test_invalid_custom_regex_raises(self):
        with pytest.raises(ArcLLMConfigError, match="Invalid regex"):
            RegexPiiDetector(custom_patterns=[{"name": "BAD", "pattern": r"[invalid"}])


# ---------------------------------------------------------------------------
# redact_text
# ---------------------------------------------------------------------------


class TestRedactText:
    def test_redact_ssn(self):
        detector = RegexPiiDetector()
        matches = detector.detect("SSN: 123-45-6789")
        result = redact_text("SSN: 123-45-6789", matches)
        assert result == "SSN: [PII:SSN]"

    def test_redact_multiple_types(self):
        detector = RegexPiiDetector()
        text = "Email user@test.com, SSN 123-45-6789"
        matches = detector.detect(text)
        result = redact_text(text, matches)
        assert "[PII:EMAIL]" in result
        assert "[PII:SSN]" in result
        assert "user@test.com" not in result
        assert "123-45-6789" not in result

    def test_redact_empty_matches(self):
        result = redact_text("No PII here", [])
        assert result == "No PII here"

    def test_redact_preserves_surrounding_text(self):
        detector = RegexPiiDetector()
        matches = detector.detect("before 123-45-6789 after")
        result = redact_text("before 123-45-6789 after", matches)
        assert result == "before [PII:SSN] after"


# ---------------------------------------------------------------------------
# Protocol conformance
# ---------------------------------------------------------------------------


class TestPiiDetectorProtocol:
    def test_regex_detector_implements_protocol(self):
        """RegexPiiDetector should satisfy PiiDetector protocol."""
        detector = RegexPiiDetector()
        # Protocol requires detect(text: str) -> list[PiiMatch]
        result = detector.detect("test")
        assert isinstance(result, list)
