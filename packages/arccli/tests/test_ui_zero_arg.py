"""Tests for `arc ui start` zero-arg behavior (SPEC-019 T3.1, T3.2, T3.3).

These exercise `_maybe_open_browser` without actually starting uvicorn — that
path is exercised by an integration test elsewhere. (SPEC-026 FR-5 removed the
per-agent trace-store discovery; arcui now reads from the shared arcstore mirror.)
"""

from __future__ import annotations

from unittest.mock import patch

from arccli.commands.ui import _maybe_open_browser

# ---------------------------------------------------------------------------
# _maybe_open_browser
# ---------------------------------------------------------------------------


class TestMaybeOpenBrowserLoopback:
    """Loopback bind opens browser with token in URL hash."""

    def test_loopback_opens_browser(self) -> None:
        with patch("webbrowser.open", return_value=True) as mock_open:
            _maybe_open_browser("127.0.0.1", 8420, "viewer_token_value")
        mock_open.assert_called_once()
        url = mock_open.call_args[0][0]
        assert "#auth=viewer_token_value" in url
        assert "127.0.0.1:8420" in url

    def test_localhost_opens_browser(self) -> None:
        with patch("webbrowser.open", return_value=True) as mock_open:
            _maybe_open_browser("localhost", 8420, "tok")
        mock_open.assert_called_once()


class TestMaybeOpenBrowserNonLoopback:
    """Non-loopback bind MUST NOT open the browser (SR-4)."""

    def test_zero_zero_zero_zero_skips(self) -> None:
        with patch("webbrowser.open") as mock_open:
            _maybe_open_browser("0.0.0.0", 8420, "tok")  # noqa: S104 — testing non-loopback path
        mock_open.assert_not_called()

    def test_external_ip_skips(self) -> None:
        with patch("webbrowser.open") as mock_open:
            _maybe_open_browser("10.0.0.1", 8420, "tok")
        mock_open.assert_not_called()
