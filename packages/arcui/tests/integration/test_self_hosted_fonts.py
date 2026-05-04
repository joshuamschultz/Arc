"""Air-gap font self-hosting (SPEC-023 §FR-26 / NFR-9).

The served HTML must reference no external CDN. fonts.css is mounted at
``/assets/fonts/fonts.css`` and declares every @font-face the dashboard
uses. WOFF2 binaries live next to the CSS and load via relative URLs.
"""

from __future__ import annotations

from pathlib import Path

from starlette.testclient import TestClient

from arcui.server import create_app

_STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "arcui" / "static"
_FONTS_DIR = _STATIC_DIR / "assets" / "fonts"


def test_no_googleapis_reference_in_served_html() -> None:
    """The served dashboard HTML must not reference fonts.googleapis.com."""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/")
        assert resp.status_code == 200
        body = resp.text.lower()
        assert "fonts.googleapis.com" not in body
        assert "fonts.gstatic.com" not in body


def test_fonts_css_is_reachable() -> None:
    """assets/fonts/fonts.css is mounted and served with the right type."""
    app = create_app()
    with TestClient(app) as client:
        resp = client.get("/assets/fonts/fonts.css")
        assert resp.status_code == 200
        assert "@font-face" in resp.text


def test_fonts_css_declares_inter_and_jetbrains_mono() -> None:
    """fonts.css declares every weight the dashboard depends on."""
    css = (_FONTS_DIR / "fonts.css").read_text(encoding="utf-8")
    assert 'font-family: "Inter"' in css
    assert "font-weight: 400" in css
    assert "font-weight: 500" in css
    assert "font-weight: 600" in css
    assert "font-weight: 700" in css
    assert 'font-family: "JetBrains Mono"' in css


def test_fonts_directory_carries_install_readme() -> None:
    """Operators get a README explaining what files belong here."""
    readme = _FONTS_DIR / "README.md"
    assert readme.exists()
    text = readme.read_text(encoding="utf-8")
    assert "Inter" in text
    assert "JetBrains Mono" in text
