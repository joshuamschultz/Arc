"""Tests for enriched PII/secret detection — checksums, gov/CUI entities, SECRETS, toggles.

Spec 015 (Content Guardrails) FR-11..FR-18. Checksum vectors are known-answer
tests (Luhn mod-10, IBAN mod-97, ABA MICR 3-7-1) — see SDD Research Insight
"Checksum rules confirmed exact".
"""

import pytest

from arcllm._pii import (
    DEFAULT_OFF_ENTITIES,
    EntityToggle,
    RegexPiiDetector,
    aba_checksum_valid,
    iban_mod97_valid,
    luhn_valid,
    redact_text,
)
from arcllm.exceptions import ArcLLMConfigError

# ---------------------------------------------------------------------------
# Checksum validators — known-answer vectors
# ---------------------------------------------------------------------------


class TestLuhnValidator:
    def test_valid_visa_test_number(self):
        assert luhn_valid("4111111111111111") is True

    def test_valid_with_separators(self):
        assert luhn_valid("4111-1111-1111-1111") is True
        assert luhn_valid("4111 1111 1111 1111") is True

    def test_invalid_last_digit_changed(self):
        assert luhn_valid("4111111111111112") is False

    def test_empty_string_invalid(self):
        assert luhn_valid("") is False


class TestIbanMod97Validator:
    def test_valid_gb_iban(self):
        assert iban_mod97_valid("GB82WEST12345698765432") is True

    def test_valid_de_iban(self):
        assert iban_mod97_valid("DE89370400440532013000") is True

    def test_invalid_checksum(self):
        assert iban_mod97_valid("GB82WEST12345698765431") is False

    def test_too_short_invalid(self):
        assert iban_mod97_valid("GB8") is False

    def test_non_alphanumeric_char_invalid(self):
        """A rearranged string with a char outside base-36 raises internally -> False."""
        assert iban_mod97_valid("GB82WEST!2345698765432") is False


class TestAbaChecksumValidator:
    def test_valid_routing_number(self):
        assert aba_checksum_valid("021000021") is True

    def test_invalid_last_digit_changed(self):
        assert aba_checksum_valid("021000022") is False

    def test_wrong_length_invalid(self):
        assert aba_checksum_valid("12345") is False


# ---------------------------------------------------------------------------
# CREDIT_CARD / IBAN / ABA_ROUTING gated by checksum (ADR-424)
# ---------------------------------------------------------------------------


class TestChecksumGatesEntityMatch:
    def test_credit_card_regex_hit_but_luhn_fails_not_a_match(self):
        """A 16-digit order number that fails Luhn must not redact as a card."""
        detector = RegexPiiDetector()
        matches = detector.detect("Order number 1234567890123456 confirmed")
        cc_matches = [m for m in matches if m.pii_type == "CREDIT_CARD"]
        assert cc_matches == []

    def test_credit_card_valid_luhn_matches(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Card: 4111111111111111")
        cc_matches = [m for m in matches if m.pii_type == "CREDIT_CARD"]
        assert len(cc_matches) == 1

    def test_iban_valid_matches(self):
        detector = RegexPiiDetector()
        matches = detector.detect("IBAN: GB82WEST12345698765432 on file")
        iban_matches = [m for m in matches if m.pii_type == "IBAN"]
        assert len(iban_matches) == 1

    def test_iban_invalid_checksum_not_a_match(self):
        detector = RegexPiiDetector()
        matches = detector.detect("IBAN: GB82WEST12345698765431 on file")
        iban_matches = [m for m in matches if m.pii_type == "IBAN"]
        assert iban_matches == []

    def test_aba_routing_valid_matches(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Routing 021000021 for wire")
        aba_matches = [m for m in matches if m.pii_type == "ABA_ROUTING"]
        assert len(aba_matches) == 1

    def test_aba_routing_invalid_checksum_not_a_match(self):
        """A random 9-digit number that fails the ABA checksum is not flagged."""
        detector = RegexPiiDetector()
        matches = detector.detect("Order 021000022 shipped")
        aba_matches = [m for m in matches if m.pii_type == "ABA_ROUTING"]
        assert aba_matches == []


# ---------------------------------------------------------------------------
# IPV6
# ---------------------------------------------------------------------------


class TestDetectIPv6:
    def test_detects_ipv6(self):
        detector = RegexPiiDetector()
        matches = detector.detect("Server at 2001:0db8:85a3:0000:0000:8a2e:0370:7334 now")
        ip6_matches = [m for m in matches if m.pii_type == "IPV6"]
        assert len(ip6_matches) == 1


# ---------------------------------------------------------------------------
# Gov/CUI entities — DEFAULT-OFF, require explicit opt-in via pii_entities.allow
# ---------------------------------------------------------------------------


class TestGovCuiEntitiesDefaultOff:
    """Per SDD Research Insight #2: no-checksum gov entities ship default-off."""

    @pytest.mark.parametrize(
        "entity_name",
        ["US_PASSPORT", "US_DRIVERS_LICENSE", "DOD_ID", "CAC", "BANK_ACCOUNT", "DOB", "MRN"],
    )
    def test_entity_is_in_default_off_set(self, entity_name):
        assert entity_name in DEFAULT_OFF_ENTITIES

    def test_passport_not_detected_by_default(self):
        detector = RegexPiiDetector()
        matches = detector.detect("My passport number: A12345678 is valid")
        assert [m for m in matches if m.pii_type == "US_PASSPORT"] == []

    def test_passport_detected_when_allow_listed(self):
        detector = RegexPiiDetector(entities={"allow": ["US_PASSPORT"]})
        matches = detector.detect("My passport number: A12345678 is valid")
        assert len(matches) == 1
        assert matches[0].pii_type == "US_PASSPORT"

    def test_drivers_license_detected_when_allow_listed(self):
        detector = RegexPiiDetector(entities={"allow": ["US_DRIVERS_LICENSE"]})
        matches = detector.detect("DL number: D1234567 on file")
        assert len(matches) == 1
        assert matches[0].pii_type == "US_DRIVERS_LICENSE"

    def test_dod_id_detected_when_allow_listed(self):
        detector = RegexPiiDetector(entities={"allow": ["DOD_ID"]})
        matches = detector.detect("EDIPI number: 1234567890 recorded")
        assert len(matches) == 1
        assert matches[0].pii_type == "DOD_ID"

    def test_cac_detected_when_allow_listed(self):
        detector = RegexPiiDetector(entities={"allow": ["CAC"]})
        matches = detector.detect("CAC number: 1234567890 issued")
        assert len(matches) == 1
        assert matches[0].pii_type == "CAC"

    def test_bank_account_detected_when_allow_listed(self):
        detector = RegexPiiDetector(entities={"allow": ["BANK_ACCOUNT"]})
        matches = detector.detect("Bank account number: 123456789012 linked")
        assert len(matches) == 1
        assert matches[0].pii_type == "BANK_ACCOUNT"

    def test_dob_detected_when_allow_listed(self):
        detector = RegexPiiDetector(entities={"allow": ["DOB"]})
        matches = detector.detect("DOB: 04/12/1985 noted")
        assert len(matches) == 1
        assert matches[0].pii_type == "DOB"

    def test_mrn_detected_when_allow_listed(self):
        detector = RegexPiiDetector(entities={"allow": ["MRN"]})
        matches = detector.detect("MRN: AB123456 filed")
        assert len(matches) == 1
        assert matches[0].pii_type == "MRN"

    def test_bare_10_digit_number_never_matches_dod_id_without_anchor(self):
        """Even when allow-listed, a bare 10-digit number needs the EDIPI/DoD-ID anchor."""
        detector = RegexPiiDetector(entities={"allow": ["DOD_ID"]})
        matches = detector.detect("Call 1234567890 today regarding your order")
        assert [m for m in matches if m.pii_type == "DOD_ID"] == []


# ---------------------------------------------------------------------------
# SECRETS category
# ---------------------------------------------------------------------------


class TestSecretsCategory:
    def test_detects_aws_access_key(self):
        detector = RegexPiiDetector()
        matches = detector.detect("key=AKIAIOSFODNN7EXAMPLE end")
        secret_matches = [m for m in matches if m.pii_type == "AWS_ACCESS_KEY"]
        assert len(secret_matches) == 1
        assert secret_matches[0].namespace == "SECRET"

    def test_detects_github_token(self):
        detector = RegexPiiDetector()
        matches = detector.detect(f"token ghp_{'a' * 36} end")
        secret_matches = [m for m in matches if m.pii_type == "GITHUB_TOKEN"]
        assert len(secret_matches) == 1

    def test_detects_jwt(self):
        detector = RegexPiiDetector()
        jwt = (
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0."
            "dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
        )
        matches = detector.detect(f"Bearer {jwt} end")
        secret_matches = [m for m in matches if m.pii_type == "JWT"]
        assert len(secret_matches) == 1

    def test_detects_pem_block(self):
        detector = RegexPiiDetector()
        pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIBOgIBAAJBAK...\n-----END RSA PRIVATE KEY-----"
        matches = detector.detect(pem)
        secret_matches = [m for m in matches if m.pii_type == "PEM_BLOCK"]
        assert len(secret_matches) == 1

    def test_detects_db_url(self):
        detector = RegexPiiDetector()
        matches = detector.detect("connect to postgres://user:pass@localhost:5432/db now")
        secret_matches = [m for m in matches if m.pii_type == "DB_URL"]
        assert len(secret_matches) == 1

    def test_secrets_redact_to_secret_tag_format(self):
        detector = RegexPiiDetector()
        text = "key=AKIAIOSFODNN7EXAMPLE end"
        matches = detector.detect(text)
        result = redact_text(text, matches)
        assert "[SECRET:AWS_ACCESS_KEY]" in result
        assert "AKIAIOSFODNN7EXAMPLE" not in result

    def test_secrets_disabled_when_denied(self):
        detector = RegexPiiDetector(entities={"deny": ["SECRETS"]})
        matches = detector.detect("key=AKIAIOSFODNN7EXAMPLE end")
        assert matches == []

    def test_secrets_enabled_by_default(self):
        """SECRETS is NOT in the default-off gov/CUI set — on by default."""
        assert "SECRETS" not in DEFAULT_OFF_ENTITIES

    def test_detects_anthropic_key(self):
        detector = RegexPiiDetector()
        key = "sk-ant-" + ("a" * 90)
        matches = detector.detect(f"key={key} end")
        secret_matches = [m for m in matches if m.pii_type == "ANTHROPIC_KEY"]
        assert len(secret_matches) == 1
        assert secret_matches[0].namespace == "SECRET"

    def test_anthropic_key_redacts(self):
        detector = RegexPiiDetector()
        key = "sk-ant-" + ("a" * 90)
        text = f"key={key} end"
        matches = detector.detect(text)
        result = redact_text(text, matches)
        assert "[SECRET:ANTHROPIC_KEY]" in result
        assert key not in result

    def test_detects_openai_key(self):
        detector = RegexPiiDetector()
        key = "sk-" + ("a" * 40)
        matches = detector.detect(f"key={key} end")
        secret_matches = [m for m in matches if m.pii_type == "OPENAI_KEY"]
        assert len(secret_matches) == 1

    def test_detects_openai_project_key(self):
        detector = RegexPiiDetector()
        key = "sk-proj-" + ("a" * 40)
        matches = detector.detect(f"key={key} end")
        secret_matches = [m for m in matches if m.pii_type == "OPENAI_KEY"]
        assert len(secret_matches) == 1

    def test_detects_google_api_key(self):
        detector = RegexPiiDetector()
        key = "AIza" + ("a" * 35)
        matches = detector.detect(f"key={key} end")
        secret_matches = [m for m in matches if m.pii_type == "GOOGLE_API_KEY"]
        assert len(secret_matches) == 1

    def test_detects_slack_token(self):
        detector = RegexPiiDetector()
        matches = detector.detect("token=xoxb-1234567890-abcdefghij end")
        secret_matches = [m for m in matches if m.pii_type == "SLACK_TOKEN"]
        assert len(secret_matches) == 1

    @pytest.mark.parametrize(
        "text",
        [
            "This is just an ordinary sentence about ants and iguanas.",
            "The invoice number is 123456 and the SKU is ABC-123.",
            "Please skim proj files and update sk8 board schedules soon.",
        ],
    )
    def test_new_secret_patterns_no_false_positive_on_ordinary_text(self, text):
        detector = RegexPiiDetector()
        matches = detector.detect(text)
        types = {m.pii_type for m in matches}
        assert not types & {
            "ANTHROPIC_KEY",
            "OPENAI_KEY",
            "GOOGLE_API_KEY",
            "SLACK_TOKEN",
        }


# ---------------------------------------------------------------------------
# pii_entities allow/deny toggle
# ---------------------------------------------------------------------------


class TestPiiEntitiesToggle:
    def test_allow_list_limits_scan_to_listed_categories(self):
        detector = RegexPiiDetector(entities={"allow": ["SSN"]})
        matches = detector.detect("SSN 123-45-6789 email user@test.com")
        types = {m.pii_type for m in matches}
        assert types == {"SSN"}

    def test_deny_list_excludes_category(self):
        detector = RegexPiiDetector(entities={"deny": ["EMAIL"]})
        matches = detector.detect("SSN 123-45-6789 email user@test.com")
        types = {m.pii_type for m in matches}
        assert "EMAIL" not in types
        assert "SSN" in types

    def test_deny_list_leaves_other_default_categories_enabled(self):
        detector = RegexPiiDetector(entities={"deny": ["IPV4"]})
        matches = detector.detect("SSN 123-45-6789 at 192.168.1.1")
        types = {m.pii_type for m in matches}
        assert "IPV4" not in types
        assert "SSN" in types

    def test_allow_wins_over_deny_when_both_given(self):
        """Edge case: both allow and deny given -> allow wins (explicit intent)."""
        detector = RegexPiiDetector(entities={"allow": ["SSN"], "deny": ["SSN"]})
        matches = detector.detect("SSN 123-45-6789")
        types = {m.pii_type for m in matches}
        assert types == {"SSN"}

    def test_no_toggle_scans_all_default_enabled_categories(self):
        detector = RegexPiiDetector()
        matches = detector.detect("SSN 123-45-6789 email user@test.com")
        types = {m.pii_type for m in matches}
        assert {"SSN", "EMAIL"}.issubset(types)

    def test_unknown_category_in_allow_list_raises(self):
        with pytest.raises(ArcLLMConfigError, match="Unknown pii_entities"):
            RegexPiiDetector(entities={"allow": ["NOT_A_REAL_CATEGORY"]})

    def test_unknown_category_in_deny_list_raises(self):
        with pytest.raises(ArcLLMConfigError, match="Unknown pii_entities"):
            RegexPiiDetector(entities={"deny": ["NOT_A_REAL_CATEGORY"]})

    def test_custom_pattern_category_usable_in_toggle(self):
        detector = RegexPiiDetector(
            custom_patterns=[{"name": "EMPLOYEE_ID", "pattern": r"EMP-\d{6}"}],
            entities={"allow": ["EMPLOYEE_ID"]},
        )
        matches = detector.detect("Employee EMP-123456, SSN 123-45-6789")
        types = {m.pii_type for m in matches}
        assert types == {"EMPLOYEE_ID"}


# ---------------------------------------------------------------------------
# Max-scan-length guard (L13) — bounds detect() cost on a huge message
# ---------------------------------------------------------------------------


class TestMaxScanLengthGuard:
    def test_match_within_cap_still_detected(self):
        from arcllm._pii import MAX_REGEX_SCAN_LENGTH

        text = ("x" * (MAX_REGEX_SCAN_LENGTH // 2)) + " SSN 123-45-6789"
        detector = RegexPiiDetector()
        matches = detector.detect(text)
        assert any(m.pii_type == "SSN" for m in matches)

    def test_match_beyond_cap_not_scanned(self):
        """Documented limitation: PII beyond the cap is not detected."""
        from arcllm._pii import MAX_REGEX_SCAN_LENGTH

        text = ("x" * (MAX_REGEX_SCAN_LENGTH + 500)) + " SSN 123-45-6789"
        detector = RegexPiiDetector()
        matches = detector.detect(text)
        assert not any(m.pii_type == "SSN" for m in matches)


# ---------------------------------------------------------------------------
# EntityToggle unit tests (direct)
# ---------------------------------------------------------------------------


class TestEntityToggleResolution:
    def test_from_config_none_returns_default_enabled(self):
        toggle = EntityToggle.from_config(None, known_categories=frozenset({"A", "B"}))
        assert toggle.enabled == frozenset({"A", "B"})

    def test_from_config_empty_dict_returns_default_enabled(self):
        toggle = EntityToggle.from_config({}, known_categories=frozenset({"A", "B"}))
        assert toggle.enabled == frozenset({"A", "B"})

    def test_from_config_respects_default_off(self):
        toggle = EntityToggle.from_config(
            None,
            known_categories=frozenset({"A", "B"}),
            default_off=frozenset({"B"}),
        )
        assert toggle.enabled == frozenset({"A"})
