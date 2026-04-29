"""Cross-language constant-sync regression tests (review TD-HIGH).

The Python constant `arcui._constants.BOOTSTRAP_HASH_KEY` and the literal
parsed by the JS bootstrap in `static/index.html` are coupled by
convention only. A rename in Python without updating the HTML breaks the
auth handoff with no test failure outside a full integration browser
test. These tests assert the convention loudly so a rename trips CI.
"""

from __future__ import annotations

import re
from pathlib import Path

from arcui._constants import BOOTSTRAP_HASH_KEY, LOOPBACK_HOSTS

_INDEX_HTML = Path(__file__).parent.parent / "src" / "arcui" / "static" / "index.html"


class TestBootstrapHashKeySync:
    def test_index_html_parses_the_python_hash_key(self) -> None:
        """The hash-bootstrap regex in index.html MUST match BOOTSTRAP_HASH_KEY."""
        html = _INDEX_HTML.read_text()
        # The bootstrap script contains: hash.match(/[#&]auth=([^&]+)/)
        # Where 'auth' must equal BOOTSTRAP_HASH_KEY.
        pattern = rf"hash\.match\(/\[#&\]{re.escape(BOOTSTRAP_HASH_KEY)}=\(\[\^&\]\+\)/\)"
        assert re.search(pattern, html), (
            f"index.html bootstrap regex must use the literal "
            f"'{BOOTSTRAP_HASH_KEY}=' (from arcui._constants); a rename in "
            "Python without updating the HTML breaks browser auth handoff."
        )


class TestLoopbackHostsSingleSource:
    """LOOPBACK_HOSTS is the only source of truth — check both former
    duplicate-call-site files import from `arcui._constants`, not their own
    locally-defined frozenset."""

    def _packages_root(self) -> Path:
        # tests/ → arcui/ → packages/
        return Path(__file__).parent.parent.parent

    def test_arccli_imports_from_constants(self) -> None:
        ui_py = self._packages_root() / "arccli" / "src" / "arccli" / "commands" / "ui.py"
        text = ui_py.read_text()
        assert "from arcui._constants import" in text
        assert "LOOPBACK_HOSTS" in text
        # No locally-defined frozenset of loopback addrs.
        assert 'frozenset({"127.0.0.1"' not in text

    def test_ui_reporter_imports_from_constants(self) -> None:
        ur_py = (
            self._packages_root()
            / "arcagent"
            / "src"
            / "arcagent"
            / "modules"
            / "ui_reporter"
            / "__init__.py"
        )
        text = ur_py.read_text()
        assert "from arcui._constants import LOOPBACK_HOSTS" in text
        assert 'frozenset({"127.0.0.1"' not in text

    def test_loopback_hosts_contents(self) -> None:
        """Sanity check: the canonical set covers IPv4 + name + IPv6 loopback."""
        assert "127.0.0.1" in LOOPBACK_HOSTS
        assert "localhost" in LOOPBACK_HOSTS
        assert "::1" in LOOPBACK_HOSTS
