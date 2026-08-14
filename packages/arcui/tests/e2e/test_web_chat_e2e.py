"""Air-gap check on the served static bundle.

The full chat turn that used to live here is now ``tests/journeys/`` — it was
gated on ``ARC_E2E=1`` *and* on a ``team/concierge_agent`` directory absent from
every checkout, so it never ran and never caught anything. The journey suite
drives the same pipe against a real agent with a scripted model, by default.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_air_gap_no_external_references_in_static_bundle() -> None:
    """No served static asset references an external CDN.

    Walks every file under ``arcui/static/`` and asserts no occurrence
    of common CDN hostnames. Air-gap-ready (NFR-9 / FR-26).
    """
    static_root = Path(__file__).resolve().parents[2] / "src" / "arcui" / "static"
    suspicious = re.compile(
        r"(fonts\.googleapis\.com|fonts\.gstatic\.com|cdnjs\.cloudflare\.com|"
        r"cdn\.jsdelivr\.net|unpkg\.com|fontawesome\.com)"
    )
    # Only HTML/CSS/JS files actually load assets in a browser; READMEs and
    # other docs are not request-path content even though StaticFiles will
    # serve them on direct GET.
    browser_suffixes = {".html", ".css", ".js"}
    offenders: list[tuple[Path, str]] = []
    for path in static_root.rglob("*"):
        if not path.is_file() or path.suffix.lower() not in browser_suffixes:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        match = suspicious.search(text)
        if match:
            offenders.append((path.relative_to(static_root), match.group(0)))
    assert not offenders, "external CDN references found in served static bundle:\n" + "\n".join(
        f"  {p}: {m}" for p, m in offenders
    )
