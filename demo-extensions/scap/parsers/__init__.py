"""SCAP scanner output parsers (SPEC-024 § SDD §2).

Three formats supported:
  * STIG Viewer CSV  → stig_csv.parse
  * XCCDF 1.2 XML    → xccdf_xml.parse  (OpenSCAP, SCC)
  * SCC HTML report  → scc_html.parse   (DISA SCC fallback)

Each parser returns ``list[Finding]`` with original (unsanitized) host
identifiers — sanitization runs as a separate step in ``sanitize.py``.
CCIs and NIST 800-53 Rev 4/Rev 5 mappings are preserved verbatim.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

Format = Literal["stig_csv", "xccdf_xml", "scc_html"]


def detect_format(path: str | Path) -> Format:
    """Detect SCAP source format from extension and content sniff.

    Raises ``ValueError`` on unsupported / undetectable input so the
    caller can return a clean ``Error: ...`` string to the LLM.
    """
    p = Path(path)
    if not p.exists():
        raise ValueError(f"File not found: {path}")
    suffix = p.suffix.lower()
    head = p.read_text(encoding="utf-8", errors="replace")[:4096]
    if suffix == ".csv" and ('"Benchmark Name"' in head or '"Rule ID"' in head):
        return "stig_csv"
    if suffix == ".xml" and ("xccdf" in head.lower() or "<Benchmark" in head):
        return "xccdf_xml"
    if suffix in (".html", ".htm") and ("SCC" in head or "scc" in head.lower() or "Compliance" in head):
        return "scc_html"
    raise ValueError(
        f"Could not determine format for {path}. "
        "Supported: STIG CSV, XCCDF XML, SCC HTML."
    )


__all__ = ["detect_format", "Format"]
