"""sanitize_error_text — a failure reason an operator can read and a log can keep."""

from __future__ import annotations

from arctrust import sanitize_error_text


def test_keeps_class_and_message() -> None:
    assert sanitize_error_text("ValueError: tool exploded") == "ValueError: tool exploded"


def test_redacts_secrets_urls_and_emails() -> None:
    text = (
        "HTTPError: 401 at https://api.example.com/v1?key=abc "
        "token sk-ant-api03-abcdefghijklmnopqrstuvwxyz0123456789 by bob@example.com"
    )
    out = sanitize_error_text(text)
    assert "api.example.com" not in out
    assert "sk-ant" not in out
    assert "bob@example.com" not in out
    assert "HTTPError: 401" in out


def test_caps_size_and_collapses_whitespace() -> None:
    out = sanitize_error_text("boom\n\n" + "x " * 5000, limit=100)
    assert len(out) <= 100
    assert "\n" not in out


def test_empty_input_is_empty() -> None:
    assert sanitize_error_text("") == ""
