"""Unit tests for DeliveryTarget parser.

Tests cover:
- Two-segment format: platform:chat_id
- Three-segment format: platform:chat_id:thread_id
- Normalisation (lowercase platform, whitespace stripping)
- Round-trip serialisation via __str__
- Error cases: too few segments, empty chat_id
- Unknown platform warning (should not raise)
"""

from __future__ import annotations

import pytest

from arcgateway.delivery import DeliveryTarget


class TestDeliveryTargetParse:
    def test_two_segment_format(self) -> None:
        """telegram:12345 parses to platform=telegram, chat_id=12345, thread_id=None."""
        target = DeliveryTarget.parse("telegram:12345")
        assert target.platform == "telegram"
        assert target.chat_id == "12345"
        assert target.thread_id is None

    def test_three_segment_format(self) -> None:
        """telegram:12345:67890 parses with all three fields."""
        target = DeliveryTarget.parse("telegram:12345:67890")
        assert target.platform == "telegram"
        assert target.chat_id == "12345"
        assert target.thread_id == "67890"

    def test_slack_channel(self) -> None:
        """slack:C123ABC parses correctly."""
        target = DeliveryTarget.parse("slack:C123ABC")
        assert target.platform == "slack"
        assert target.chat_id == "C123ABC"

    def test_slack_with_thread(self) -> None:
        """slack:C123ABC:T9876 parses with thread_id."""
        target = DeliveryTarget.parse("slack:C123ABC:T9876")
        assert target.platform == "slack"
        assert target.chat_id == "C123ABC"
        assert target.thread_id == "T9876"

    def test_platform_normalised_to_lowercase(self) -> None:
        """Platform name is lowercased regardless of input case."""
        target = DeliveryTarget.parse("Telegram:12345")
        assert target.platform == "telegram"

    def test_empty_thread_id_becomes_none(self) -> None:
        """Empty third segment should be treated as absent thread_id."""
        # "telegram:12345:" has an empty third segment
        target = DeliveryTarget.parse("telegram:12345:")
        assert target.thread_id is None

    def test_missing_separator_raises(self) -> None:
        """String without any colon raises ValueError."""
        with pytest.raises(ValueError, match="Invalid DeliveryTarget"):
            DeliveryTarget.parse("telegram")

    def test_empty_string_raises(self) -> None:
        """Empty string raises ValueError."""
        with pytest.raises(ValueError):
            DeliveryTarget.parse("")

    def test_empty_chat_id_raises(self) -> None:
        """Empty chat_id raises a validation error."""
        with pytest.raises(ValueError, match="chat_id"):
            DeliveryTarget.parse("telegram:")

    def test_unknown_platform_does_not_raise(self) -> None:
        """Unknown platform logs a warning but does not raise."""
        # Should NOT raise — unknown adapters may be registered at runtime
        target = DeliveryTarget.parse("mattermost:my_channel")
        assert target.platform == "mattermost"
        assert target.chat_id == "my_channel"


class TestDeliveryTargetStr:
    def test_round_trip_without_thread(self) -> None:
        """__str__ round-trips a two-segment address."""
        original = "telegram:12345"
        target = DeliveryTarget.parse(original)
        assert str(target) == original

    def test_round_trip_with_thread(self) -> None:
        """__str__ round-trips a three-segment address."""
        original = "telegram:12345:67890"
        target = DeliveryTarget.parse(original)
        assert str(target) == original

    def test_str_omits_thread_id_when_none(self) -> None:
        """__str__ produces two-segment form when thread_id is None."""
        target = DeliveryTarget(platform="slack", chat_id="C123", thread_id=None)
        assert str(target) == "slack:C123"

    def test_str_includes_thread_id_when_set(self) -> None:
        """__str__ produces three-segment form when thread_id is set."""
        target = DeliveryTarget(platform="slack", chat_id="C123", thread_id="T99")
        assert str(target) == "slack:C123:T99"


class TestDeliveryTargetConstruction:
    def test_direct_construction(self) -> None:
        """DeliveryTarget can be constructed directly without parse()."""
        target = DeliveryTarget(platform="discord", chat_id="guild:channel")
        assert target.platform == "discord"
        assert target.chat_id == "guild:channel"
        assert target.thread_id is None

    def test_all_known_platforms_accepted(self) -> None:
        """All known platforms parse without warnings or errors."""
        platforms = ["telegram", "slack", "discord", "whatsapp", "signal", "matrix", "email"]
        for p in platforms:
            target = DeliveryTarget.parse(f"{p}:testchat")
            assert target.platform == p
