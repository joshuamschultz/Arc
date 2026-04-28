"""Shared constants — single source of truth for cross-module + cross-language values.

These constants are referenced from multiple Python packages (`arccli`,
`arcagent`) and from the JavaScript bootstrap in `static/index.html`.
Keeping a single Python module as the source means a rename in one place
either updates everyone (via import) or breaks loudly via the
`tests/test_bootstrap_constants_sync.py` regression test that asserts
the JS side still uses the same literal.

Pillar 2: a security-relevant set with two writers is a CWE-710
waiting to happen. Pillar 1: one constant, one place.
"""

from __future__ import annotations

from typing import Final

# Loopback bind addresses. Used by:
#   - `arccli.commands.ui._maybe_open_browser` (gates webbrowser.open; SR-4)
#   - `arcagent.modules.ui_reporter._server_reachable` (rejects non-loopback
#     URLs before any HTTP probe; review H-4)
#   - any future code that needs the "are we on a loopback bind?" decision
LOOPBACK_HOSTS: Final[frozenset[str]] = frozenset({"127.0.0.1", "localhost", "::1"})

# URL hash key for browser bootstrap auth. The server emits
# `http://host:port/#auth=<viewer_token>` and the JS bootstrap parses
# `[#&]auth=([^&]+)`. The literal MUST match between server and client —
# `tests/integration/test_bootstrap_constants_sync.py` asserts the HTML
# still uses this exact key, so a rename here trips CI before reaching
# the browser.
BOOTSTRAP_HASH_KEY: Final[str] = "auth"
