"""Pydantic models for the SCAP extension (SPEC-024 § SDD §5)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Severity = Literal["high", "medium", "low", "informational", "unknown"]
Status = Literal["pass", "fail", "notchecked", "notapplicable", "error"]
ScannerSource = Literal["stig_csv", "xccdf_xml", "scc_html"]
Baseline = Literal["low", "moderate", "high"]


class Finding(BaseModel):
    """A single rule-result from a SCAP scan, sanitized at ingest."""

    rule_id: str
    title: str = ""
    severity: Severity = "unknown"
    status: Status = "notchecked"
    ccis: list[str] = Field(default_factory=list)
    nist_800_53_rev4: list[str] = Field(default_factory=list)
    nist_800_53_rev5: list[str] = Field(default_factory=list)
    fix_text: str | None = None
    discussion: str | None = None
    host_alias: str
    scanner_source: ScannerSource

    @property
    def all_controls(self) -> list[str]:
        """Union of Rev 4 and Rev 5 controls (deduped, order-preserved)."""
        seen: dict[str, None] = {}
        for c in (*self.nist_800_53_rev5, *self.nist_800_53_rev4):
            seen.setdefault(c, None)
        return list(seen.keys())


class IngestResult(BaseModel):
    """Output of one `scap_ingest` call — cached in _state._INGESTS."""

    host_alias: str
    scanner_source: ScannerSource
    findings: list[Finding]
    ingested_at: str  # ISO 8601
    source_path: str


class GapEntry(BaseModel):
    """One control gap from `scap_baseline_compare`."""

    control: str
    baseline: Baseline
    failing_rules: list[str] = Field(default_factory=list)
    severity_score: float = 0.0
    effort_tshirt: Literal["S", "M", "L", "XL"] = "M"


class AttackTechnique(BaseModel):
    """One MITRE ATT&CK technique from `scap_attack_correlate`."""

    technique_id: str  # e.g. "T1110.001"
    name: str
    tactic: str
    url: str
    related_controls: list[str] = Field(default_factory=list)
    threat_narrative: str = ""
