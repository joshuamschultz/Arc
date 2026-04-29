"""Tests for the JS bootstrap snippet in index.html (SPEC-019 T3.5).

We cannot easily run a full headless browser in CI, so this test parses the
HTML, extracts the bootstrap script, and verifies its observable contract:
  - Reads `auth=` from location.hash
  - Writes to localStorage as `arcui_viewer_token`
  - Calls history.replaceState to strip the hash
  - Runs synchronously, before any other <script> tag

If the page later swaps to a JS module bundler, this test would migrate to a
jsdom-based runner; for now, structural assertions are sufficient.
"""

from __future__ import annotations

import re
from pathlib import Path

_INDEX_HTML = Path(__file__).resolve().parents[2] / "src" / "arcui" / "static" / "index.html"


class TestBootstrapPlacement:
    """Bootstrap MUST be the first <script> in <head> (SR-2)."""

    def test_bootstrap_in_head(self) -> None:
        text = _INDEX_HTML.read_text()
        head_close = text.find("</head>")
        assert head_close > 0
        head = text[:head_close]
        assert "bootstrapAuth" in head, (
            "bootstrap function must live inside <head> so it runs before "
            "fonts/styles/page scripts"
        )

    def test_bootstrap_is_first_script(self) -> None:
        text = _INDEX_HTML.read_text()
        head_end = text.find("</head>")
        head = text[:head_end]
        # First script tag must be the bootstrap; any tracker placed before
        # would defeat SR-2 (token reachable via window.location.hash).
        first_script = re.search(r"<script[^>]*>", head, flags=re.IGNORECASE)
        assert first_script is not None
        snippet = head[first_script.end() : head.find("</script>", first_script.end())]
        assert "arcui_viewer_token" in snippet


class TestBootstrapBehaviorContract:
    """The script must implement the SR-2 contract."""

    def test_reads_hash_writes_localstorage(self) -> None:
        text = _INDEX_HTML.read_text()
        # Hash parsing on auth=
        assert re.search(r"location\.hash", text) is not None
        assert re.search(r"auth=", text) is not None
        # localStorage write
        assert (
            "localStorage.setItem('arcui_viewer_token'" in text
            or 'localStorage.setItem("arcui_viewer_token"' in text
        )

    def test_calls_history_replace_state(self) -> None:
        text = _INDEX_HTML.read_text()
        assert "history.replaceState" in text


class TestApiClientCarriesAuth:
    """fetch() and fetchAPI() carry the viewer token (T3.6)."""

    def test_authorization_bearer_header(self) -> None:
        text = _INDEX_HTML.read_text()
        # The auth-header helper exists.
        assert "Authorization" in text
        assert "Bearer " in text
        # localStorage is the source of truth.
        assert (
            "localStorage.getItem('arcui_viewer_token')" in text
            or 'localStorage.getItem("arcui_viewer_token")' in text
        )
