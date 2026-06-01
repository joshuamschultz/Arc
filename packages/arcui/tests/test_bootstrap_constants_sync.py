"""Cross-language constant-sync regression tests (review TD-HIGH).

The Python constant `arcui._constants.BOOTSTRAP_HASH_KEY` and the literal
parsed by the JS bootstrap in `web/src/lib/auth.ts` (compiled into the built
JS bundle) are coupled by convention only. A rename in Python without updating
the JS breaks the auth handoff with no test failure outside a full integration
browser test. These tests assert the convention loudly so a rename trips CI.
"""

from __future__ import annotations

import re
from pathlib import Path

from arcui._constants import BOOTSTRAP_HASH_KEY, LOOPBACK_HOSTS

_ASSETS_DIR = Path(__file__).parent.parent / "src" / "arcui" / "static" / "assets"


class TestBootstrapHashKeySync:
    def test_bundle_parses_the_python_hash_key(self) -> None:
        """The hash-bootstrap regex in the built JS bundle MUST match
        BOOTSTRAP_HASH_KEY. The React app bootstraps auth in `lib/auth.ts`
        (`hash.match(/[#&]auth=([^&]+)/)`); Vite content-hashes the bundle
        filename, but the regex literal survives minification."""
        bundle = "\n".join(p.read_text() for p in _ASSETS_DIR.glob("*.js"))
        # Matches `[#&]auth=([^&]+)` regardless of the surrounding minified code.
        pattern = rf"\[#&\]{re.escape(BOOTSTRAP_HASH_KEY)}=\(\[\^&\]\+\)"
        assert re.search(pattern, bundle), (
            f"built JS bundle must contain the '{BOOTSTRAP_HASH_KEY}=' hash "
            "regex (from arcui._constants); a rename in Python without updating "
            "lib/auth.ts breaks browser auth handoff."
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

    def test_loopback_hosts_contents(self) -> None:
        """Sanity check: the canonical set covers IPv4 + name + IPv6 loopback."""
        assert "127.0.0.1" in LOOPBACK_HOSTS
        assert "localhost" in LOOPBACK_HOSTS
        assert "::1" in LOOPBACK_HOSTS
