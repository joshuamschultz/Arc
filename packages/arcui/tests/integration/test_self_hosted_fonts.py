"""Air-gap font self-hosting (SPEC-023 §FR-26 / NFR-9).

No external font CDN may be referenced anywhere. The React frontend self-hosts
Plus Jakarta Sans + IBM Plex Mono via @fontsource: Vite bundles the WOFF2
binaries into ``/assets`` and inlines the ``@font-face`` rules into the built
CSS bundle. These tests assert that contract — served HTML and built CSS carry
no CDN URL, and the WOFF2 binaries are present on disk.
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from arcui.server import create_app

_STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "arcui" / "static"
_ASSETS_DIR = _STATIC_DIR / "assets"


def test_no_font_cdn_reference_in_served_html() -> None:
    """The served dashboard HTML must not reference any font CDN."""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.text.lower()
        assert "fonts.googleapis.com" not in body
        assert "fonts.gstatic.com" not in body


def test_built_css_self_hosts_fonts() -> None:
    """The built CSS bundle defines @font-face rules pointing at local
    /assets WOFF2 files — never an external CDN."""
    css_files = list(_ASSETS_DIR.glob("*.css"))
    assert css_files, "no built CSS bundle found — run `npm run build`"
    css = "\n".join(p.read_text(encoding="utf-8") for p in css_files)
    assert "@font-face" in css, "built CSS has no @font-face rules"
    assert "fonts.googleapis.com" not in css
    assert "fonts.gstatic.com" not in css


def test_woff2_binaries_present() -> None:
    """The self-hosted WOFF2 binaries are bundled into /assets."""
    woff2 = list(_ASSETS_DIR.glob("*.woff2"))
    assert woff2, "no WOFF2 font binaries in static/assets — fonts not self-hosted"
    names = " ".join(p.name for p in woff2).lower()
    assert "jakarta" in names, "Plus Jakarta Sans WOFF2 missing"
    assert "plex-mono" in names or "plexmono" in names, "IBM Plex Mono WOFF2 missing"
