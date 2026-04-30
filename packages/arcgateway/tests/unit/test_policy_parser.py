"""Tests for arcgateway.policy_parser — pure ACE bullet parser.

Format under test:
    - [P##] <text> {score:N, uses:N, reviewed:YYYY-MM-DD, created:YYYY-MM-DD, source:<sid>}
"""

from __future__ import annotations

from datetime import date

from arcgateway.policy_parser import PolicyBullet, parse_bullets


class TestWellFormed:
    def test_single_well_formed_bullet(self) -> None:
        text = (
            "- [P01] Always validate caller_did at the trust boundary "
            "{score:8, uses:14, reviewed:2026-04-20, created:2026-04-01, source:s-2026-04-01-1234}"
        )
        bullets = parse_bullets(text)
        assert len(bullets) == 1
        b = bullets[0]
        assert b.id == "P01"
        assert b.text == "Always validate caller_did at the trust boundary"
        assert b.score == 8
        assert b.uses == 14
        assert b.reviewed == date(2026, 4, 20)
        assert b.created == date(2026, 4, 1)
        assert b.source == "s-2026-04-01-1234"
        assert b.retired is False

    def test_multiple_bullets(self) -> None:
        text = (
            "- [P01] Bullet one {score:7, uses:1, reviewed:2026-04-01, created:2026-03-01, source:s-A}\n"
            "- [P02] Bullet two {score:5, uses:2, reviewed:2026-04-02, created:2026-03-02, source:s-B}\n"
        )
        bullets = parse_bullets(text)
        assert len(bullets) == 2
        assert bullets[0].id == "P01"
        assert bullets[1].id == "P02"

    def test_returns_PolicyBullet_instances(self) -> None:
        text = "- [P01] Text {score:5, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s-X}"
        bullets = parse_bullets(text)
        assert isinstance(bullets[0], PolicyBullet)


class TestMissingFields:
    def test_missing_uses_defaults_to_zero(self) -> None:
        text = "- [P01] Text {score:5, reviewed:2026-04-01, created:2026-04-01, source:s-X}"
        bullets = parse_bullets(text)
        assert bullets[0].uses == 0

    def test_missing_score_defaults_to_5(self) -> None:
        text = "- [P01] Text {uses:0, reviewed:2026-04-01, created:2026-04-01, source:s-X}"
        bullets = parse_bullets(text)
        assert bullets[0].score == 5

    def test_missing_reviewed_returns_none(self) -> None:
        text = "- [P01] Text {score:5, uses:0, created:2026-04-01, source:s-X}"
        bullets = parse_bullets(text)
        assert bullets[0].reviewed is None

    def test_missing_created_returns_none(self) -> None:
        text = "- [P01] Text {score:5, uses:0, reviewed:2026-04-01, source:s-X}"
        bullets = parse_bullets(text)
        assert bullets[0].created is None

    def test_missing_source_returns_empty_string(self) -> None:
        text = "- [P01] Text {score:5, uses:0, reviewed:2026-04-01, created:2026-04-01}"
        bullets = parse_bullets(text)
        assert bullets[0].source == ""


class TestRetiredFlag:
    def test_score_zero_is_retired(self) -> None:
        text = "- [P01] Text {score:0, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}"
        assert parse_bullets(text)[0].retired is True

    def test_score_one_is_retired(self) -> None:
        text = "- [P01] Text {score:1, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}"
        assert parse_bullets(text)[0].retired is True

    def test_score_two_is_retired(self) -> None:
        text = "- [P01] Text {score:2, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}"
        assert parse_bullets(text)[0].retired is True

    def test_score_three_is_active(self) -> None:
        text = "- [P01] Text {score:3, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}"
        assert parse_bullets(text)[0].retired is False

    def test_score_ten_is_active(self) -> None:
        text = "- [P01] Text {score:10, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}"
        b = parse_bullets(text)[0]
        assert b.score == 10
        assert b.retired is False


class TestEdgeCases:
    def test_negative_score_is_retired(self) -> None:
        text = "- [P01] Text {score:-1, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}"
        b = parse_bullets(text)[0]
        assert b.score == -1
        assert b.retired is True

    def test_malformed_line_skipped(self) -> None:
        text = (
            "this is just a regular line\n"
            "- [P01] Good {score:5, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}\n"
            "- not a bullet without id\n"
            "## Section header\n"
        )
        bullets = parse_bullets(text)
        assert len(bullets) == 1
        assert bullets[0].id == "P01"

    def test_empty_text_returns_empty_list(self) -> None:
        assert parse_bullets("") == []

    def test_invalid_date_returns_none(self) -> None:
        text = "- [P01] Text {score:5, uses:0, reviewed:not-a-date, created:2026-04-01, source:s}"
        b = parse_bullets(text)[0]
        assert b.reviewed is None
        assert b.created == date(2026, 4, 1)

    def test_indented_bullet_parsed(self) -> None:
        text = "    - [P01] Indented {score:5, uses:0, reviewed:2026-04-01, created:2026-04-01, source:s}"
        bullets = parse_bullets(text)
        assert len(bullets) == 1
        assert bullets[0].id == "P01"

    def test_text_with_internal_spaces(self) -> None:
        text = "- [P42] This is a bullet with many   spaces and punctuation. {score:7, uses:3, reviewed:2026-04-01, created:2026-04-01, source:s}"
        b = parse_bullets(text)[0]
        assert b.text == "This is a bullet with many   spaces and punctuation."
        assert b.id == "P42"
