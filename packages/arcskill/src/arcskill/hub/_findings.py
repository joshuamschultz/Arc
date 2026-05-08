"""Scanner result types.

Lifted out of ``arcskill.hub.scanner`` so the regex/text and AST sibling
modules can yield ``Finding`` objects without creating a circular import
(scanner imports the passes; the passes need the result type).

Re-exported through ``arcskill.hub.scanner`` — callers continue to do
``from arcskill.hub.scanner import Finding, ScanResult``.
"""

from __future__ import annotations

from typing import NamedTuple


class Finding(NamedTuple):
    """One scanner finding.

    Attributes
    ----------
    severity:
        ``"critical"``, ``"high"``, ``"medium"``, or ``"low"``.
    category:
        One of the 8 Hermes categories + ``"text_injection"``.
    rule_id:
        Short rule identifier (e.g. ``"curl_pipe_shell"``).
    message:
        Human-readable explanation.
    path:
        File path within the bundle where found (empty for archive-level).
    line:
        Line number within *path* (0 if unknown).
    """

    severity: str
    category: str
    rule_id: str
    message: str
    path: str
    line: int


class ScanResult(NamedTuple):
    """Aggregated scanner output.

    Attributes
    ----------
    verdict:
        ``"safe"``, ``"caution"``, or ``"dangerous"``.
    findings:
        All individual findings (sorted by severity descending).
    counts:
        ``{severity: count}`` mapping.
    scanner_passes:
        List of scanner passes that ran (e.g. ``["regex", "bandit"]``).
    """

    verdict: str
    findings: list[Finding]
    counts: dict[str, int]
    scanner_passes: list[str]
