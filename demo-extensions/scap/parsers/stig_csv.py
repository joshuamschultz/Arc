"""STIG Viewer CSV parser.

Handles Palo Alto NDM, Cisco NX-OS NDM, and any STIG Viewer CSV export.
The first line of these files is a classification banner
(``~~~~~~~ Unclassified ~~~~~~``) which we skip; the second line is the
header row.

The ``CCIs`` cell contains multiple line-separated entries:

    CCI-000054
    Limit the number of concurrent sessions ...
    NIST SP 800-53::AC-10
    NIST SP 800-53A::AC-10.1 (ii)
    NIST SP 800-53 Revision 4::AC-10
    NIST SP 800-53 Revision 5::AC-10

Multiple CCIs are separated by blank lines. Mappings are pulled from the
entire cell regardless of chunking — order is preserved, duplicates
deduplicated.

Status values from STIG Viewer:
    "Open"            -> fail
    "NotAFinding" / "Not a Finding"     -> pass
    "Not_Applicable" / "Not Applicable" -> notapplicable
    "Not_Reviewed"   / "Not Reviewed"   -> notchecked
"""

from __future__ import annotations

import csv
import re
from pathlib import Path

from ..models import Finding, ScannerSource, Severity, Status

_SCANNER: ScannerSource = "stig_csv"

_STATUS_MAP: dict[str, Status] = {
    "open": "fail",
    "notafinding": "pass",
    "not a finding": "pass",
    "not_applicable": "notapplicable",
    "not applicable": "notapplicable",
    "not_reviewed": "notchecked",
    "not reviewed": "notchecked",
    "": "notchecked",
}

_SEVERITY_VALUES: set[Severity] = {"high", "medium", "low", "informational", "unknown"}


def _normalize_status(raw: str) -> Status:
    return _STATUS_MAP.get(raw.strip().lower(), "notchecked")


def _normalize_severity(raw: str) -> Severity:
    s = (raw or "").strip().lower()
    if s in _SEVERITY_VALUES:
        return s  # type: ignore[return-value]
    return "unknown"


_CCI_RE = re.compile(r"^CCI-\d+$")
_REV4_RE = re.compile(r"^NIST\s+SP\s+800-53\s+Revision\s+4::(.+)$", re.IGNORECASE)
_REV5_RE = re.compile(r"^NIST\s+SP\s+800-53\s+Revision\s+5::(.+)$", re.IGNORECASE)
_GENERIC_RE = re.compile(r"^NIST\s+SP\s+800-53::(.+)$", re.IGNORECASE)


def _parse_ccis_cell(cell: str) -> tuple[list[str], list[str], list[str]]:
    """Return (ccis, rev4_controls, rev5_controls) — deduped, order-preserved."""
    ccis: list[str] = []
    rev4: list[str] = []
    rev5: list[str] = []
    generic: list[str] = []
    seen_cci: set[str] = set()
    seen_4: set[str] = set()
    seen_5: set[str] = set()
    seen_g: set[str] = set()
    for raw in (cell or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if _CCI_RE.match(line):
            if line not in seen_cci:
                ccis.append(line)
                seen_cci.add(line)
            continue
        m = _REV5_RE.match(line)
        if m:
            ctrl = m.group(1).strip()
            if ctrl not in seen_5:
                rev5.append(ctrl)
                seen_5.add(ctrl)
            continue
        m = _REV4_RE.match(line)
        if m:
            ctrl = m.group(1).strip()
            if ctrl not in seen_4:
                rev4.append(ctrl)
                seen_4.add(ctrl)
            continue
        m = _GENERIC_RE.match(line)
        if m:
            ctrl = m.group(1).strip()
            if ctrl not in seen_g:
                generic.append(ctrl)
                seen_g.add(ctrl)
            continue
    # Generic "NIST SP 800-53::" entries (no Rev) — fall back into Rev 4 if Rev 4 is empty
    if not rev4 and generic:
        rev4 = generic
    return ccis, rev4, rev5


def parse(path: str | Path, host_alias: str) -> list[Finding]:
    """Parse a STIG Viewer CSV into a list of Findings."""
    p = Path(path)
    findings: list[Finding] = []
    with p.open(encoding="utf-8", errors="replace") as f:
        first = f.readline()
        # Skip classification banner if present; otherwise rewind
        if not first.lstrip().startswith("~"):
            f.seek(0)
        reader = csv.DictReader(f)
        for row in reader:
            rule_id = (row.get("Rule ID") or "").strip()
            if not rule_id:
                continue
            ccis, rev4, rev5 = _parse_ccis_cell(row.get("CCIs", ""))
            findings.append(
                Finding(
                    rule_id=rule_id,
                    title=(row.get("Rule Title") or "").strip(),
                    severity=_normalize_severity(row.get("Severity", "")),
                    status=_normalize_status(row.get("Status", "")),
                    ccis=ccis,
                    nist_800_53_rev4=rev4,
                    nist_800_53_rev5=rev5,
                    fix_text=(row.get("Fix Text") or None),
                    discussion=(row.get("Discussion") or None),
                    host_alias=host_alias,
                    scanner_source=_SCANNER,
                )
            )
    return findings
