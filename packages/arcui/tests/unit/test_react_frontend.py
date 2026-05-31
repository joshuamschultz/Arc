"""Cutover contract for the React frontend (replaces the vanilla-JS asset tests).

After the SPA rebuild, the Python server serves a Vite build: a single
content-hashed JS + CSS bundle under ``/assets`` referenced from a minimal
``index.html``, plus a one-time kill-switch service worker. These tests pin
that contract so a broken or missing build is caught before a browser.
"""

from __future__ import annotations

import re
from pathlib import Path

from starlette.testclient import TestClient

from arcui.server import create_app

_STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "arcui" / "static"


def test_index_is_react_mount_point() -> None:
    app = create_app()
    with TestClient(app) as client:
        html = client.get("/").text
        assert '<div id="root">' in html, "missing React mount node"
        assert "/assets/index-" in html, "no hashed bundle referenced"


def test_no_vanilla_assets_remain() -> None:
    """The old IIFE modules must be gone from disk (cutover complete)."""
    for name in ("arc-shell.js", "store.js", "ws-client.js", "arc-platform.css"):
        assert not (_STATIC_DIR / "assets" / name).exists(), f"stale {name} remains"


def test_sw_is_killswitch_not_cache() -> None:
    """The service worker must self-unregister, not cache the shell — Vite's
    content hashing handles cache-busting, and a caching SW would resurrect
    the old vanilla app for returning visitors."""
    sw = (_STATIC_DIR / "sw.js").read_text(encoding="utf-8")
    assert "unregister" in sw, "sw.js must unregister itself"
    assert "caches.delete" in sw, "sw.js must purge caches"
    assert "addEventListener('fetch'" not in sw, "kill-switch SW must not intercept fetches"


def test_bundle_present_and_nonempty() -> None:
    app = create_app()
    with TestClient(app) as client:
        assets = re.findall(r'(?:src|href)="(/assets/[^"]+)"', client.get("/").text)
        js = [a for a in assets if a.endswith(".js")]
        css = [a for a in assets if a.endswith(".css")]
        assert js and css, "index.html must reference both a JS and a CSS bundle"
        for path in js + css:
            resp = client.get(path)
            assert resp.status_code == 200 and resp.content, f"{path} broken"
