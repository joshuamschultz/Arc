"""SCAP extension for Arc — NLIT 2026 demo (SPEC-024).

Wraps OpenSCAP / SCC / STIG Viewer outputs into a queryable model the
agent reasons over for ATO evidence assembly, baseline gap analysis,
drift detection, and MITRE ATT&CK threat correlation.

All 6 tools are read_only. Sanitization runs at ingest. PDF rendering
via WeasyPrint requires `brew install pango cairo gdk-pixbuf` on macOS.
"""

__version__ = "1.0.0"
