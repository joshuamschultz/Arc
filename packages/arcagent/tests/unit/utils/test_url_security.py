from __future__ import annotations

import pytest

from arcagent.utils.url_security import UnsafeURLError, validate_http_url


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "https://user:secret@example.com/",
        "https://example.com/\nHost: internal",
        "http://127.0.0.1/",
        "http://[::1]/",
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/",
        "http://224.0.0.1/",
    ],
)
def test_rejects_unsafe_urls(url: str) -> None:
    with pytest.raises(UnsafeURLError):
        validate_http_url(url)


def test_resolution_rejects_any_non_public_answer() -> None:
    with pytest.raises(UnsafeURLError, match="non-public"):
        validate_http_url(
            "https://example.com",
            resolve=True,
            resolver=lambda _host: ["93.184.216.34", "127.0.0.1"],
        )


def test_resolution_accepts_only_public_answers() -> None:
    validated = validate_http_url(
        "https://EXAMPLE.com./path",
        resolve=True,
        resolver=lambda _host: ["93.184.216.34"],
    )
    assert validated.hostname == "example.com"
