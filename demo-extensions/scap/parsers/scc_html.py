"""SCC 5.14 HTML report parser.

DISA SCC emits per-rule HTML tables of key-value rows like::

    Rule ID:      xccdf_mil.disa.stig_rule_SV-205739r958726_rule
    Rule Result:  Not Applicable
    Identities:   SV-103117  V-93029
                  CCI-002235 (NIST SP 800-53 Rev 4: AC-6 (10); NIST SP 800-53 Rev 5: AC-6 (10))
    Description:  ...
    Fix Text:     ...
    Severity:     high

CCIs and 800-53 mappings live inside the "Identities:" cell as
parenthesized prose. The parser walks every table that has a "Rule ID:"
row, treats its rows as a key-value sheet, and extracts mappings from
the Identities cell with a small set of regexes.
"""

from __future__ import annotations

import re
from pathlib import Path

from bs4 import BeautifulSoup, Tag

from ..models import Finding, ScannerSource, Severity, Status

_SCANNER: ScannerSource = "scc_html"

_STATUS_MAP: dict[str, Status] = {
    "pass": "pass",
    "fail": "fail",
    "not applicable": "notapplicable",
    "notapplicable": "notapplicable",
    "not reviewed": "notchecked",
    "notreviewed": "notchecked",
    "not selected": "notchecked",
    "notselected": "notchecked",
    "error": "error",
}

_SEVERITY_VALUES: set[Severity] = {"high", "medium", "low", "informational", "unknown"}

_CCI_RE = re.compile(r"(CCI-\d{6})")
# Capture greedily up to the next "NIST SP 800-53" / ";" / end; clean trailing
# unbalanced ")" via _strip_trailing_unmatched_parens. Control IDs themselves
# can contain "(N)" enhancement suffixes (e.g. "AC-6 (10)").
_REV4_RE = re.compile(
    r"NIST\s+SP\s+800-53(?:\s+Rev(?:ision)?)?\s+4:\s*(.+?)"
    r"(?=\s*;|\s*NIST\s+SP|\s*CCI-\d|\s*$)",
    re.IGNORECASE,
)
_REV5_RE = re.compile(
    r"NIST\s+SP\s+800-53(?:\s+Rev(?:ision)?)?\s+5:\s*(.+?)"
    r"(?=\s*;|\s*NIST\s+SP|\s*CCI-\d|\s*$)",
    re.IGNORECASE,
)
_RULE_ID_RE = re.compile(r"(SV-\d+(?:r\d+)?)")


def _strip_trailing_unmatched_parens(s: str) -> str:
    s = s.strip()
    while s.endswith(")") and s.count(")") > s.count("("):
        s = s[:-1].rstrip()
    return s


def _normalize_status(raw: str) -> Status:
    return _STATUS_MAP.get((raw or "").strip().lower(), "notchecked")


def _normalize_severity(raw: str) -> Severity:
    s = (raw or "").strip().lower()
    if s in _SEVERITY_VALUES:
        return s  # type: ignore[return-value]
    return "unknown"


def _row_kv(table: Tag) -> dict[str, Tag]:
    """Return a {label_lower_no_colon: value_cell} dict for the table's top-level rows."""
    out: dict[str, Tag] = {}
    for tr in table.find_all("tr", recursive=False):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 2:
            continue
        label = tds[0].get_text(" ", strip=True).rstrip(":").lower()
        if label and label not in out:
            out[label] = tds[1]
    return out


def _parse_identities(cell: Tag) -> tuple[str, list[str], list[str], list[str]]:
    """Return (rule_id_short, ccis, rev4, rev5)."""
    text = cell.get_text(" ", strip=True)
    rule_id_short = ""
    m = _RULE_ID_RE.search(text)
    if m:
        rule_id_short = m.group(1)
    ccis: list[str] = []
    seen_cci: set[str] = set()
    for cci in _CCI_RE.findall(text):
        if cci not in seen_cci:
            ccis.append(cci)
            seen_cci.add(cci)
    rev4: list[str] = []
    seen_4: set[str] = set()
    for raw in _REV4_RE.findall(text):
        ctrl = _strip_trailing_unmatched_parens(raw)
        if ctrl and ctrl not in seen_4:
            rev4.append(ctrl)
            seen_4.add(ctrl)
    rev5: list[str] = []
    seen_5: set[str] = set()
    for raw in _REV5_RE.findall(text):
        ctrl = _strip_trailing_unmatched_parens(raw)
        if ctrl and ctrl not in seen_5:
            rev5.append(ctrl)
            seen_5.add(ctrl)
    return rule_id_short, ccis, rev4, rev5


def parse(path: str | Path, host_alias: str) -> list[Finding]:
    """Parse an SCC HTML report into Findings."""
    p = Path(path)
    html = p.read_text(encoding="utf-8", errors="replace")
    soup = BeautifulSoup(html, "html.parser")

    findings: list[Finding] = []
    seen_rule_ids: set[str] = set()

    for table in soup.find_all("table"):
        # Heuristic: a rule table contains a "Rule ID:" label row near its top
        first_label_cell = table.find("td", string=re.compile(r"^\s*Rule ID:\s*$"))
        if not first_label_cell:
            continue
        kv = _row_kv(table)
        rule_id_full_cell = kv.get("rule id")
        if rule_id_full_cell is None:
            continue
        rule_id_full = rule_id_full_cell.get_text(" ", strip=True)
        if not rule_id_full:
            continue
        if rule_id_full in seen_rule_ids:
            continue
        seen_rule_ids.add(rule_id_full)

        identities_cell = kv.get("identities")
        rule_id_short = ""
        ccis: list[str] = []
        rev4: list[str] = []
        rev5: list[str] = []
        if identities_cell is not None:
            rule_id_short, ccis, rev4, rev5 = _parse_identities(identities_cell)

        result_text = (
            kv["rule result"].get_text(" ", strip=True) if "rule result" in kv else ""
        )
        severity_text = (
            kv["severity"].get_text(" ", strip=True) if "severity" in kv else ""
        )
        title = ""
        if "rule" in kv and kv["rule"] is not None:
            title = kv["rule"].get_text(" ", strip=True)
        if not title and "version" in kv:
            title = kv["version"].get_text(" ", strip=True)

        description = (
            kv["description"].get_text(" ", strip=True) if "description" in kv else None
        )
        fix_text = (
            kv["fix text"].get_text(" ", strip=True) if "fix text" in kv else None
        )

        findings.append(
            Finding(
                rule_id=rule_id_short or rule_id_full,
                title=title or "",
                severity=_normalize_severity(severity_text),
                status=_normalize_status(result_text),
                ccis=ccis,
                nist_800_53_rev4=rev4,
                nist_800_53_rev5=rev5,
                fix_text=fix_text,
                discussion=description,
                host_alias=host_alias,
                scanner_source=_SCANNER,
            )
        )

    return findings
