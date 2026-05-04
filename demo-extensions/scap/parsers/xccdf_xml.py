"""XCCDF 1.2 XML parser (OpenSCAP / SCC output).

Reads a Benchmark + TestResult and joins each ``<rule-result>`` to its
``<Rule>`` definition. Mappings come from ``<reference>`` elements:

  * ``href`` containing ``800-53r4.pdf`` → Rev 4, text is the control ID
  * ``href`` containing ``800-53r5.pdf`` → Rev 5
  * ``ident`` with system referencing CCI → CCI ID (CCEs are skipped — they
    are configuration enumerations, not CCIs)

XCCDF rule-result statuses are mapped to the ``Finding.status`` enum:

  ``pass`` → pass     ``fail`` → fail        ``error`` → error
  ``notapplicable`` → notapplicable          ``notchecked`` → notchecked
  ``notselected`` → notchecked  (rule not in active profile)
  ``unknown`` / ``informational`` → notchecked
  ``fixed`` → pass    (rule failed but was remediated in the same run)
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

from lxml import etree

from ..models import Finding, ScannerSource, Severity, Status

_SCANNER: ScannerSource = "xccdf_xml"
_NS: Final = {"x": "http://checklists.nist.gov/xccdf/1.2"}

_STATUS_MAP: dict[str, Status] = {
    "pass": "pass",
    "fail": "fail",
    "error": "error",
    "unknown": "notchecked",
    "notapplicable": "notapplicable",
    "notchecked": "notchecked",
    "notselected": "notchecked",
    "informational": "notchecked",
    "fixed": "pass",
}

_SEVERITY_VALUES: set[Severity] = {"high", "medium", "low", "informational", "unknown"}


def _normalize_severity(raw: str | None) -> Severity:
    s = (raw or "").strip().lower()
    if s in _SEVERITY_VALUES:
        return s  # type: ignore[return-value]
    return "unknown"


def _normalize_status(raw: str | None) -> Status:
    return _STATUS_MAP.get((raw or "").strip().lower(), "notchecked")


def _extract_text(elem: etree._Element | None) -> str:
    if elem is None:
        return ""
    # Concatenate all text including child element text (titles can contain xhtml)
    return " ".join(elem.itertext()).strip()


def _is_cci_system(system: str | None) -> bool:
    s = (system or "").lower()
    return "cci" in s and "cce" not in s


def _is_rev4_href(href: str | None) -> bool:
    h = (href or "").lower()
    return "800-53r4" in h or "800-53-r4" in h


def _is_rev5_href(href: str | None) -> bool:
    h = (href or "").lower()
    return "800-53r5" in h or "800-53-r5" in h


def _collect_rule_metadata(rule: etree._Element) -> dict[str, object]:
    """Pull title, severity, references, idents from a Rule definition."""
    title = _extract_text(rule.find("x:title", _NS))
    severity = _normalize_severity(rule.get("severity"))
    rev4: list[str] = []
    rev5: list[str] = []
    ccis: list[str] = []
    seen_4: set[str] = set()
    seen_5: set[str] = set()
    seen_cci: set[str] = set()

    for ref in rule.findall("x:reference", _NS):
        text = (ref.text or "").strip()
        if not text:
            continue
        href = ref.get("href")
        if _is_rev5_href(href) and text not in seen_5:
            rev5.append(text)
            seen_5.add(text)
        elif _is_rev4_href(href) and text not in seen_4:
            rev4.append(text)
            seen_4.add(text)

    for ident in rule.findall("x:ident", _NS):
        text = (ident.text or "").strip()
        if not text:
            continue
        if _is_cci_system(ident.get("system")) and text not in seen_cci:
            ccis.append(text)
            seen_cci.add(text)

    discussion_el = rule.find("x:description", _NS)
    discussion = _extract_text(discussion_el) if discussion_el is not None else None

    fix_el = rule.find("x:fixtext", _NS)
    fix_text = _extract_text(fix_el) if fix_el is not None else None

    return {
        "title": title,
        "severity": severity,
        "ccis": ccis,
        "rev4": rev4,
        "rev5": rev5,
        "discussion": discussion or None,
        "fix_text": fix_text or None,
    }


def parse(path: str | Path, host_alias: str) -> list[Finding]:
    """Parse an XCCDF 1.2 result file into Findings."""
    p = Path(path)
    tree = etree.parse(str(p))
    root = tree.getroot()

    # Build a rule_id -> metadata map (single pass over Rule elements)
    rules: dict[str, dict[str, object]] = {}
    for rule in root.findall(".//x:Rule", _NS):
        rid = rule.get("id")
        if not rid:
            continue
        rules[rid] = _collect_rule_metadata(rule)

    # Iterate rule-results in TestResult — these are the actual scan outcomes
    findings: list[Finding] = []
    for tr in root.findall(".//x:TestResult", _NS):
        for rr in tr.findall("x:rule-result", _NS):
            rid = rr.get("idref")
            if not rid:
                continue
            meta = rules.get(rid, {})
            result_el = rr.find("x:result", _NS)
            status = _normalize_status(_extract_text(result_el))
            severity_override = rr.get("severity")
            severity: Severity = (
                _normalize_severity(severity_override)
                if severity_override
                else meta.get("severity", "unknown")  # type: ignore[arg-type]
            )
            findings.append(
                Finding(
                    rule_id=rid,
                    title=meta.get("title", "") or "",  # type: ignore[arg-type]
                    severity=severity,
                    status=status,
                    ccis=list(meta.get("ccis", []) or []),  # type: ignore[arg-type]
                    nist_800_53_rev4=list(meta.get("rev4", []) or []),  # type: ignore[arg-type]
                    nist_800_53_rev5=list(meta.get("rev5", []) or []),  # type: ignore[arg-type]
                    fix_text=meta.get("fix_text"),  # type: ignore[arg-type]
                    discussion=meta.get("discussion"),  # type: ignore[arg-type]
                    host_alias=host_alias,
                    scanner_source=_SCANNER,
                )
            )
    return findings
