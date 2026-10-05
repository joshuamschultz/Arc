"""sanitize_error_text — a failure reason an operator can read and a log can keep."""

from __future__ import annotations

import pytest

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


_JWT = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r_wW1gFWFOEjXk"
)
_PEM = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo\n"
    "-----END RSA PRIVATE KEY-----"
)

# (text, secret that must not survive)
_MUST_REDACT = [
    ("401 Authorization: Bearer abc123DEF456ghi789", "abc123DEF456ghi789"),
    ("header authorization=bearer tok_9f8e7d6c5b4a3210", "tok_9f8e7d6c5b4a3210"),
    ("sent bearer xyz.789-abc_def~ghi+jkl/mno=", "xyz.789-abc_def~ghi+jkl/mno="),
    ("login failed password=hunter2hunter2", "hunter2hunter2"),
    ("login failed Password: 'S3cretPhrase!'", "S3cretPhrase!"),
    ("bad passwd=s3cr3tvalue&user=bob", "s3cr3tvalue"),
    ("config secret=0123456789abcdef", "0123456789abcdef"),
    ("failed ?token=abcDEF123456&x=1", "abcDEF123456"),
    ("failed &api_key=k_live_998877665544", "k_live_998877665544"),
    ("ftp://alice:p4ssw0rd@files.example.com/x", "p4ssw0rd"),
    ("proxy http://alice:p4ssw0rd@10.0.0.1:8080", "p4ssw0rd"),
    (f"key material {_PEM} end", "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo"),
    ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    ("AWS_SECRET_ACCESS_KEY: wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "wJalrXUtnFEMI"),
    (f"jwt rejected {_JWT}", _JWT),
    ("slack said " + "xoxb" + "-" + "123456789012-abcdefghijklmnop", "abcdefghijklmnop"),
    ("slack said " + "xoxp" + "-" + "123456789012-abcdefghijklmnop", "abcdefghijklmnop"),
    ("slack said " + "xoxa" + "-" + "123456789012-abcdefghijklmnop", "abcdefghijklmnop"),
]

# Ordinary text that must come through unchanged.
_MUST_KEEP = [
    "The password field is required",
    "reset your password via the settings page",
    "token limit exceeded for this request",
    "bearer of bad news: upstream timeout",
    "secret santa list is empty",
    "Authorization header missing",
    "invalid token",
    "passwd file not found",
    "ConnectionError: host unreachable after 3 retries",
    "version 1.2.3 of eyJ is unsupported",
]


@pytest.mark.parametrize(("text", "secret"), _MUST_REDACT)
def test_secret_shapes_are_redacted(text: str, secret: str) -> None:
    out = sanitize_error_text(text)
    assert secret not in out, out


@pytest.mark.parametrize("text", _MUST_KEEP)
def test_ordinary_text_is_not_over_redacted(text: str) -> None:
    assert sanitize_error_text(text) == text
