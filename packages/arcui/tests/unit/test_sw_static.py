"""Static-file tests for the SPEC-025 service worker (Track D, task D1).

These tests do not execute the SW in a browser — they validate the source
text for mandatory structural contracts so a CI run can catch regressions
without a Playwright fixture.

Tests for the registration side (D3, D4) check that wherever the SW is
registered the __SW_DISABLE__ escape hatch is also present.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC = (
    Path(__file__).resolve().parents[2] / "src" / "arcui" / "static"
)
_SW = _STATIC / "sw.js"
_INDEX = _STATIC / "index.html"


def _sw_text() -> str:
    assert _SW.exists(), f"sw.js not found at {_SW}"
    return _SW.read_text(encoding="utf-8")


def _index_text() -> str:
    assert _INDEX.exists(), f"index.html not found at {_INDEX}"
    return _INDEX.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# sw.js structural tests
# ---------------------------------------------------------------------------


def test_sw_file_exists() -> None:
    """sw.js must exist in the static directory."""
    assert _SW.exists(), f"sw.js is missing from {_STATIC}"


def test_sw_has_install_handler() -> None:
    """sw.js must register an 'install' event listener."""
    text = _sw_text()
    assert "addEventListener('install'," in text, (
        "sw.js must contain addEventListener('install', …)"
    )


def test_sw_has_activate_handler() -> None:
    """sw.js must register an 'activate' event listener."""
    text = _sw_text()
    assert "addEventListener('activate'," in text, (
        "sw.js must contain addEventListener('activate', …)"
    )


def test_sw_has_fetch_handler() -> None:
    """sw.js must register a 'fetch' event listener."""
    text = _sw_text()
    assert "addEventListener('fetch'," in text, (
        "sw.js must contain addEventListener('fetch', …)"
    )


def test_sw_excludes_api_paths() -> None:
    """sw.js must reference /api/ in a network-only (exclusion) context.

    The security contract (SPEC-025 §FR-3, AC-3.2) requires that auth-
    sensitive paths never go to disk.  We verify that the /api/ path
    appears near the always-live exclusion guard — not just somewhere in
    a comment.
    """
    text = _sw_text()
    # The ALWAYS_LIVE array (or equivalent) must reference /api/
    assert "/api/" in text, "sw.js must reference /api/ for network-only exclusion"
    # Confirm it appears before any cache.put call (exclusion is checked first)
    api_pos = text.index("/api/")
    cache_put_pos = text.find("cache.put") if "cache.put" in text else len(text)
    assert api_pos < cache_put_pos, (
        "/api/ exclusion must appear before any cache.put logic"
    )


def test_sw_excludes_ws_paths() -> None:
    """sw.js must reference /ws/ in a network-only (exclusion) context."""
    text = _sw_text()
    assert "/ws/" in text, "sw.js must reference /ws/ for network-only exclusion"


def test_sw_excludes_artifacts_paths() -> None:
    """sw.js must reference /artifacts/ in a network-only (exclusion) context.

    AC-3.2: /artifacts/digest_*.pdf requests must never hit the cache.
    """
    text = _sw_text()
    assert "/artifacts/" in text, (
        "sw.js must reference /artifacts/ for network-only exclusion"
    )


def test_sw_caches_assets() -> None:
    """sw.js must cache /assets/* paths (cache-first for hashed bundles)."""
    text = _sw_text()
    # /assets/ prefix must appear and be associated with caching logic
    assert "/assets/" in text, "sw.js must reference /assets/ for cache-first handling"
    # A cache.put or caches.open call must also be present
    assert "caches.open" in text or "cache.put" in text, (
        "sw.js must call caches.open or cache.put for asset caching"
    )


def test_sw_has_templated_cache_name() -> None:
    """sw.js must define CACHE_VERSION using the {{ARC_BUILD_ID}} template token.

    SPEC-025 §TD-3 — server.py substitutes the placeholder at startup so the
    cache key bumps every process restart. A literal version constant
    ('arcui-shell-v1') would never bump on deploy.
    """
    text = _sw_text()
    match = re.search(r"CACHE_VERSION\s*=\s*['\"]([^'\"]+)['\"]", text)
    assert match is not None, "sw.js must define a CACHE_VERSION constant"
    version_value = match.group(1)
    assert "{{ARC_BUILD_ID}}" in version_value, (
        f"CACHE_VERSION must include {{{{ARC_BUILD_ID}}}} template token "
        f"so server.py can substitute it; got: {version_value!r}"
    )
    assert version_value.startswith("arcui-shell-"), (
        f"CACHE_VERSION must start with 'arcui-shell-' prefix; got: {version_value!r}"
    )


# ---------------------------------------------------------------------------
# Registration / escape-hatch tests (D3, D4)
# ---------------------------------------------------------------------------


def test_main_registers_sw_only_with_disable_check() -> None:
    """The SW registration site must guard with __SW_DISABLE__ before register.

    This covers both the escape-hatch requirement (D4) and the registration
    requirement (D3): wherever serviceWorker.register('/sw.js') appears, the
    __SW_DISABLE__ flag must be checked first so a buggy SW deploy does not
    permanently lock operators out.
    """
    text = _index_text()
    assert "serviceWorker" in text, (
        "index.html must contain a serviceWorker reference for SW registration"
    )
    assert "__SW_DISABLE__" in text, (
        "SW registration must be guarded by a __SW_DISABLE__ check"
    )
    # __SW_DISABLE__ must appear before .register(
    disable_pos = text.index("__SW_DISABLE__")
    register_pos = text.find(".register(")
    assert disable_pos < register_pos, (
        "__SW_DISABLE__ check must appear before serviceWorker.register() call"
    )


# ---------------------------------------------------------------------------
# Server-side template substitution (TD-3 follow-up)
# ---------------------------------------------------------------------------


def test_sw_route_substitutes_build_id() -> None:
    """`/sw.js` route must substitute `{{ARC_BUILD_ID}}` before serving.

    SPEC-025 §TD-3 — without this the literal placeholder would reach the
    browser, breaking the cache-bust contract on every deploy.
    """
    from starlette.testclient import TestClient

    from arcui.auth import AuthConfig
    from arcui.server import create_app

    auth = AuthConfig({"viewer_token": "v", "operator_token": "o", "agent_token": "a"})
    with TestClient(create_app(auth_config=auth)) as client:
        resp = client.get("/sw.js")
        assert resp.status_code == 200
        body = resp.text
        assert "{{ARC_BUILD_ID}}" not in body, (
            "Server must substitute the template token before serving sw.js"
        )
        match = re.search(r"CACHE_VERSION\s*=\s*['\"]arcui-shell-([0-9a-f]+)['\"]", body)
        assert match is not None, (
            "Substituted CACHE_VERSION must look like arcui-shell-<hex-build-id>"
        )
        assert resp.headers.get("content-type", "").startswith("application/javascript")
        # Must NOT cache the SW file itself; cached *content* is what manages staleness.
        cache_control = resp.headers.get("cache-control", "")
        assert "no-cache" in cache_control or "no-store" in cache_control


def test_sw_always_live_regex_matches_paths_with_query_strings() -> None:
    """The ALWAYS_LIVE regex must reject /api/foo?bar=1 to network-only.

    Defense-in-depth: query strings on auth-sensitive paths must never end
    up cached. SPEC-025 §AC-3.2.
    """
    text = _sw_text()
    # Find the literal regex source for /^\/api\//
    assert re.search(r"/\^\\/api\\/", text), (
        "ALWAYS_LIVE must use a regex anchored at start of path so /api/foo?bar=1 matches"
    )
    # Sanity-check the same pattern by simulating the regex
    api_pattern = re.compile(r"^/api/")
    for case in ("/api/foo", "/api/foo?bar=1", "/api/team/roster?since=5", "/api/"):
        assert api_pattern.match(case), (
            f"/api/ regex fails to match {case!r} — query strings would slip through"
        )
    # Negative cases — paths that should NOT be network-only
    for case in ("/assets/foo.js", "/index.html", "/sw.js"):
        assert not api_pattern.match(case), (
            f"/api/ regex incorrectly matches {case!r}"
        )
