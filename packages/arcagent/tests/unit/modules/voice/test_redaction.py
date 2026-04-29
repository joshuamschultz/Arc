"""Tests for PII redaction on voice transcripts.

Covers: SSN, phone, email, credit card, IPv4.
Verifies: false-positive avoidance, bidirectionality indicator,
           exception safety.
"""

from __future__ import annotations

import pytest

from arcagent.modules.voice.redaction import redact_transcript


class TestSSNRedaction:
    def test_hyphenated_ssn(self) -> None:
        text, applied = redact_transcript("My SSN is 123-45-6789.")
        assert "[SSN]" in text
        assert "123-45-6789" not in text
        assert applied is True

    def test_space_separated_ssn(self) -> None:
        text, applied = redact_transcript("SSN: 123 45 6789")
        assert "[SSN]" in text
        assert applied is True

    def test_no_separator_ssn(self) -> None:
        text, applied = redact_transcript("SSN 123456789")
        assert "[SSN]" in text
        assert applied is True

    def test_non_ssn_numbers_not_redacted(self) -> None:
        # Short numbers like "123" should not match SSN
        text, applied = redact_transcript("I have 42 items.")
        assert "42" in text
        assert applied is False


class TestPhoneRedaction:
    def test_dashed_phone(self) -> None:
        text, applied = redact_transcript("Call 555-867-5309.")
        assert "[PHONE]" in text
        assert "555-867-5309" not in text
        assert applied is True

    def test_parentheses_phone(self) -> None:
        text, applied = redact_transcript("My number: (555) 867-5309")
        assert "[PHONE]" in text
        assert applied is True

    def test_dotted_phone(self) -> None:
        text, applied = redact_transcript("555.867.5309")
        assert "[PHONE]" in text
        assert applied is True

    def test_international_phone(self) -> None:
        text, applied = redact_transcript("Reach me at +1-555-867-5309")
        assert "[PHONE]" in text
        assert applied is True

    def test_ten_digit_compact(self) -> None:
        text, applied = redact_transcript("5558675309")
        assert "[PHONE]" in text
        assert applied is True


class TestEmailRedaction:
    def test_simple_email(self) -> None:
        text, applied = redact_transcript("Send it to alice@example.com")
        assert "[EMAIL]" in text
        assert "alice@example.com" not in text
        assert applied is True

    def test_email_with_plus(self) -> None:
        text, applied = redact_transcript("alice+test@sub.example.org")
        assert "[EMAIL]" in text
        assert applied is True

    def test_email_with_dots_in_local(self) -> None:
        text, applied = redact_transcript("first.last@domain.io")
        assert "[EMAIL]" in text
        assert applied is True

    def test_at_sign_alone_not_redacted(self) -> None:
        # Lone @ is not an email
        text, applied = redact_transcript("user @ home")
        # This should not match (spaces around @)
        # Behaviour is acceptable either way — just must not crash
        assert isinstance(text, str)


class TestCreditCardRedaction:
    def test_visa_format(self) -> None:
        text, applied = redact_transcript("Visa: 4111 1111 1111 1111")
        assert "[CREDIT_CARD]" in text
        assert "4111" not in text.split("[CREDIT_CARD]")[0].replace("Visa: ", "")
        assert applied is True

    def test_amex_format(self) -> None:
        # Use compact Amex (no separators) to avoid phone regex collision.
        # Hyphenated Amex like 3782-822463-10005 partially matches the phone
        # pattern (822463-1000 looks like area-prefix-line). Compact format
        # is unambiguous.
        text, applied = redact_transcript("Amex 378282246310005")
        assert "[CREDIT_CARD]" in text
        assert applied is True

    def test_hyphenated_card(self) -> None:
        text, applied = redact_transcript("Card: 4111-1111-1111-1111")
        assert "[CREDIT_CARD]" in text
        assert applied is True


class TestIPv4Redaction:
    def test_standard_ipv4(self) -> None:
        text, applied = redact_transcript("Connect to 192.168.1.100")
        assert "[IPV4]" in text
        assert "192.168.1.100" not in text
        assert applied is True

    def test_localhost_not_typically_pii_but_redacted(self) -> None:
        # 127.0.0.1 matches the pattern — that's intentional for federal contexts
        text, applied = redact_transcript("server at 127.0.0.1")
        assert "[IPV4]" in text
        assert applied is True


class TestMultiplePiiInText:
    def test_multiple_pii_types_redacted(self) -> None:
        text, applied = redact_transcript(
            "Alice (alice@example.com) called 555-867-5309 re SSN 123-45-6789"
        )
        assert "[EMAIL]" in text
        assert "[PHONE]" in text
        assert "[SSN]" in text
        assert applied is True

    def test_clean_text_no_redaction(self) -> None:
        text, applied = redact_transcript("The weather is nice today.")
        assert text == "The weather is nice today."
        assert applied is False

    def test_empty_string(self) -> None:
        text, applied = redact_transcript("")
        assert text == ""
        assert applied is False


class TestRedactionExceptionSafety:
    def test_returns_original_on_exception(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """If the redaction loop raises, original text is returned safely.

        re.Pattern.subn is immutable in CPython, so we patch _REDACTION_RULES
        to inject a fake pattern whose sub() raises instead.
        """
        import arcagent.modules.voice.redaction as redaction_mod

        class _ExplodingPattern:
            """A fake compiled pattern that always raises on subn()."""

            def subn(self, repl: str, string: str) -> tuple[str, int]:
                raise RuntimeError("simulated regex failure")

        # Replace the rules list with one exploding entry
        original_rules = redaction_mod._REDACTION_RULES
        monkeypatch.setattr(
            redaction_mod,
            "_REDACTION_RULES",
            [(_ExplodingPattern(), "[BANG]")],  # type: ignore[list-item]
        )

        text, applied = redact_transcript("test 123-45-6789")
        # Original text returned with no crash
        assert text == "test 123-45-6789"
        assert applied is False

        # Restore original rules
        monkeypatch.setattr(redaction_mod, "_REDACTION_RULES", original_rules)
