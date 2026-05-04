"""Evidence pack tool — assembles ATO control narrative PDF + POA&M CSV.

PDF rendering uses WeasyPrint (D-369). On macOS this requires
``brew install pango cairo gdk-pixbuf`` — the tool fails loud with that
instruction if the system libraries are missing.
"""

from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path
from typing import Any

from arcagent.tools._decorator import tool

from . import _data, _state
from .models import Finding

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_FAMILY_NAMES = {
    "AC": "Access Control",
    "AT": "Awareness and Training",
    "AU": "Audit and Accountability",
    "CA": "Assessment, Authorization, and Monitoring",
    "CM": "Configuration Management",
    "CP": "Contingency Planning",
    "IA": "Identification and Authentication",
    "IR": "Incident Response",
    "MA": "Maintenance",
    "MP": "Media Protection",
    "PE": "Physical and Environmental Protection",
    "PL": "Planning",
    "PS": "Personnel Security",
    "RA": "Risk Assessment",
    "SA": "System and Services Acquisition",
    "SC": "System and Communications Protection",
    "SI": "System and Information Integrity",
    "SR": "Supply Chain Risk Management",
}


def _norm_control(c: str) -> str:
    """Normalize control IDs (strip whitespace inside parens, etc.)."""
    return re.sub(r"\s+\(", "(", c).strip()


def _gather_for_family(family: str, baseline: str) -> dict[str, Any]:
    """Aggregate findings across the ingest cache by control within a family."""
    family_upper = family.upper()
    baseline_set = set(_data.fedramp_baselines().get(baseline, []))

    by_control: dict[str, list[Finding]] = defaultdict(list)
    hosts_seen: set[str] = set()
    for r in _state.all():
        hosts_seen.add(r.host_alias)
        for f in r.findings:
            if f.status not in ("fail", "error"):
                continue
            ctls = (f.nist_800_53_rev5 or []) + (f.nist_800_53_rev4 or [])
            for ctl in ctls:
                norm = _norm_control(ctl)
                if not norm.startswith(family_upper + "-"):
                    continue
                if norm not in baseline_set:
                    # Still include — useful for narrative — but flag below
                    pass
                by_control[norm].append(f)

    return {
        "by_control": by_control,
        "hosts": sorted(hosts_seen),
        "baseline_set": baseline_set,
    }


def _build_control_block(ctl: str, findings: list[Finding], baseline_set: set[str]) -> dict[str, Any]:
    title = _data.control_title(ctl) or "(title unavailable in bundled catalog)"
    sevs = Counter(f.severity for f in findings)
    hosts = sorted({f.host_alias for f in findings})
    in_baseline = ctl in baseline_set
    rule_samples = sorted({
        f.rule_id.replace("xccdf_org.ssgproject.content_rule_", "")
        for f in findings
    })[:8]
    findings_table = []
    for f in sorted(findings, key=lambda x: (x.severity != "high", x.rule_id))[:10]:
        findings_table.append({
            "rule": f.rule_id.replace("xccdf_org.ssgproject.content_rule_", ""),
            "severity": f.severity,
            "status": f.status,
            "host": f.host_alias,
        })
    ccis = sorted({c for f in findings for c in f.ccis})[:8]

    # Auto-generate the narrative paragraph
    high_count = sevs.get("high", 0)
    sev_summary = ", ".join(f"{n} {s}" for s, n in sevs.most_common())
    narrative = (
        f"Control {ctl} — {title} — currently shows {len(findings)} non-compliant "
        f"finding(s) across {len(hosts)} host(s) "
        f"({', '.join(hosts)}). Severity distribution: {sev_summary}. "
        f"This control "
        f"{'IS' if in_baseline else 'IS NOT'} included in the FedRAMP baseline scope. "
        + (
            f"With {high_count} high-severity finding(s), remediation is required before "
            f"submission. " if high_count else ""
        )
        + "The supporting findings table below cites the specific rule IDs from genuine "
          "SCAP scan output; remediation guidance lives in each rule's `fix_text` field "
          "as captured by the scanner."
    )

    return {
        "id": ctl,
        "title": title,
        "status_label": "Non-Compliant — Remediation Required" if findings else "Not Implemented",
        "hosts_affected": hosts,
        "fail_count": len(findings),
        "narrative": narrative,
        "findings": findings_table,
        "ccis": ccis,
    }


def _ensure_macos_brew_libs() -> None:
    """On Apple Silicon, brew installs glib/cairo/pango under /opt/homebrew/lib.
    Python's bundled CPython doesn't search there by default, so weasyprint's
    ``ctypes.util.find_library`` fails to locate libgobject-2.0-0. Setting
    DYLD_FALLBACK_LIBRARY_PATH before the first dlopen fixes this."""
    import os
    import platform
    if platform.system() != "Darwin":
        return
    brew_lib = "/opt/homebrew/lib"
    if not Path(brew_lib).is_dir():
        return
    existing = os.environ.get("DYLD_FALLBACK_LIBRARY_PATH", "")
    if brew_lib in existing.split(":"):
        return
    os.environ["DYLD_FALLBACK_LIBRARY_PATH"] = (
        f"{brew_lib}:{existing}" if existing else brew_lib
    )


def _render_pdf(context: dict[str, Any], output_path: Path) -> Path:
    """Render the ATO narrative HTML → PDF via WeasyPrint."""
    _ensure_macos_brew_libs()
    try:
        import weasyprint  # noqa: WPS433  — top-level for clear errors
        from jinja2 import Environment, FileSystemLoader, select_autoescape
    except ImportError as e:
        raise RuntimeError(
            f"Missing Python dependency: {e}. "
            "Run: uv pip install --python .venv/bin/python weasyprint jinja2"
        )

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATES_DIR)),
        autoescape=select_autoescape(["html"]),
    )
    template = env.get_template("ato_narrative.html")
    html = template.render(**context)

    try:
        weasyprint.HTML(string=html, base_url=str(_TEMPLATES_DIR)).write_pdf(str(output_path))
    except OSError as e:
        # Most common: missing pango/cairo system libraries on macOS
        raise RuntimeError(
            "WeasyPrint failed to render PDF. On macOS this usually means missing "
            "system libraries. Run:\n"
            "    brew install pango cairo gdk-pixbuf libffi\n"
            f"Underlying error: {e}"
        )
    return output_path


def _render_poam_csv(family: str, baseline: str, by_control: dict[str, list[Finding]],
                     baseline_set: set[str], output_path: Path) -> Path:
    """Write FedRAMP-style POA&M CSV from gap data."""
    today = date.today()
    rows = [[
        "POA&M Item ID",
        "Weakness Name",
        "Weakness Description",
        "Severity",
        "Source",
        "NIST 800-53 Control(s)",
        "FedRAMP Baseline",
        "In Baseline",
        "Affected Hosts",
        "Sample Rule IDs",
        "Recommended Remediation",
        "Owner (Suggested)",
        "Estimated Effort",
        "Discovered Date",
        "Target Completion",
        "Status",
    ]]

    # Sort controls by severity-weighted score descending
    def score(ctl_findings: list[Finding]) -> float:
        sev_w = {"high": 3.0, "medium": 2.0, "low": 1.0}
        return sum(sev_w.get(f.severity, 0.5) for f in ctl_findings)

    sorted_ctrls = sorted(by_control.items(), key=lambda kv: -score(kv[1]))

    for idx, (ctl, findings) in enumerate(sorted_ctrls, start=1):
        if not findings:
            continue
        sevs = Counter(f.severity for f in findings)
        max_sev = ("high" if sevs.get("high")
                   else "medium" if sevs.get("medium")
                   else "low")
        hosts = sorted({f.host_alias for f in findings})
        rules = sorted({f.rule_id.replace("xccdf_org.ssgproject.content_rule_", "") for f in findings})[:5]
        # Pick the longest fix_text as the recommended remediation
        fix_candidates = [f.fix_text for f in findings if f.fix_text]
        remediation = (max(fix_candidates, key=len)[:500]) if fix_candidates else (
            f"Configure system to satisfy {ctl} per agency hardening baseline. "
            f"See SCAP rule fix-text for specific guidance."
        )
        # Owner suggestion based on family
        family = ctl.split("-")[0] if "-" in ctl else ""
        owner_map = {
            "AC": "Identity & Access Management Team",
            "AU": "Security Operations / SIEM Team",
            "CM": "System Engineering / Configuration Management",
            "IA": "Identity & Access Management Team",
            "MA": "System Engineering",
            "SC": "Network / Cryptography Team",
            "SI": "Security Engineering / Vulnerability Management",
        }
        owner = owner_map.get(family, "System Owner")
        # Effort
        weight = len(findings) + (5 if max_sev == "high" else 2 if max_sev == "medium" else 0)
        effort = ("S" if weight <= 2 else "M" if weight <= 5 else "L" if weight <= 10 else "XL")
        # Target days based on severity
        target_days = 14 if max_sev == "high" else 60 if max_sev == "medium" else 180
        from datetime import timedelta
        target_date = today + timedelta(days=target_days)

        title = _data.control_title(ctl) or f"{ctl} non-compliance"
        rows.append([
            f"POAM-{family}-{idx:03d}",
            title,
            f"{len(findings)} non-compliant rule(s) for {ctl} across {len(hosts)} host(s).",
            max_sev.capitalize(),
            "SCAP scan (OpenSCAP / SCC / STIG Viewer)",
            ctl,
            baseline.upper(),
            "Yes" if ctl in baseline_set else "No (not in baseline scope)",
            "; ".join(hosts),
            "; ".join(rules),
            remediation,
            owner,
            effort,
            today.isoformat(),
            target_date.isoformat(),
            "Open",
        ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, quoting=csv.QUOTE_MINIMAL)
        w.writerows(rows)
    return output_path


@tool(
    name="scap_evidence_pack",
    description=(
        "Generate a full ATO evidence package for a 800-53 control family: "
        "an ATO control-narrative PDF + a FedRAMP-style POA&M CSV. PDF rendering "
        "requires WeasyPrint (and on macOS, brew install pango cairo gdk-pixbuf). "
        "Returns paths to the generated artifacts."
    ),
    classification="read_only",
    capability_tags=["compliance_check", "file_read"],
    when_to_use=(
        "When the agent needs to produce auditor-grade evidence artifacts for a "
        "control family (e.g. AC, AU, CM, SC) against a FedRAMP baseline."
    ),
    requires_skill="scap",
    version="1.0.0",
)
async def scap_evidence_pack(
    control_family: str,
    baseline: str,  # "low" | "moderate" | "high"
    output_dir: str,
    system_name: str = "Reference Federal Boundary",
    skip_pdf: bool = False,
) -> str:
    """Render ATO control-narrative PDF + POA&M CSV for a control family."""
    family = control_family.upper().strip()
    if family not in _FAMILY_NAMES:
        return (
            f"Error: control_family must be one of {sorted(_FAMILY_NAMES)} (got {control_family!r})"
        )
    baseline_lc = baseline.lower().strip()
    if baseline_lc not in ("low", "moderate", "high"):
        return f"Error: baseline must be low|moderate|high (got {baseline!r})"

    if not _state.all():
        return "Error: No hosts ingested yet. Call scap_ingest first."

    out_dir = Path(output_dir).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    gathered = _gather_for_family(family, baseline_lc)
    by_control = gathered["by_control"]
    hosts = gathered["hosts"]
    baseline_set = gathered["baseline_set"]
    if not by_control:
        return (
            f"Note: no failing findings map to family {family} for any ingested host. "
            f"Nothing to write for {family} / {baseline_lc.upper()}."
        )

    # Severity rollup for executive summary
    all_findings: list[Finding] = [f for fs in by_control.values() for f in fs]
    sev_counter = Counter(f.severity for f in all_findings)
    total = sum(sev_counter.values()) or 1
    severity_breakdown = [
        (s.capitalize(), sev_counter.get(s, 0), f"{(sev_counter.get(s, 0)/total)*100:.1f}%")
        for s in ("high", "medium", "low")
    ]

    controls_blocks = [
        _build_control_block(ctl, fs, baseline_set)
        for ctl, fs in sorted(by_control.items(), key=lambda kv: -len(kv[1]))
    ]

    context = {
        "title": f"{family} Control Family Evidence — FedRAMP {baseline_lc.upper()}",
        "system_name": system_name,
        "control_family": family,
        "family_name": _FAMILY_NAMES[family],
        "baseline": baseline_lc,
        "doc_date": date.today().isoformat(),
        "host_count": len(hosts),
        "controls_with_findings": len(controls_blocks),
        "total_failures": len(all_findings),
        "severity_breakdown": severity_breakdown,
        "controls": controls_blocks,
    }

    pdf_path = out_dir / f"{family}_evidence_{baseline_lc}.pdf"
    poam_path = out_dir / f"{family}_poam_{baseline_lc}.csv"

    pdf_status = ""
    if skip_pdf:
        pdf_status = "(skipped on caller request)"
    else:
        try:
            _render_pdf(context, pdf_path)
            pdf_status = str(pdf_path)
        except RuntimeError as e:
            pdf_status = f"PDF render failed — {e}"
        except Exception as e:  # noqa: BLE001
            pdf_status = f"PDF render failed unexpectedly — {type(e).__name__}: {e}"

    _render_poam_csv(family, baseline_lc, by_control, baseline_set, poam_path)

    return (
        f"### Evidence pack: {family} ({_FAMILY_NAMES[family]}) — FedRAMP {baseline_lc.upper()}\n\n"
        f"- **Hosts in scope**: {len(hosts)} ({', '.join(hosts)})\n"
        f"- **Failing controls**: {len(controls_blocks)}\n"
        f"- **Total failing findings**: {len(all_findings)} "
        f"(high={sev_counter.get('high', 0)}, "
        f"med={sev_counter.get('medium', 0)}, "
        f"low={sev_counter.get('low', 0)})\n"
        f"- **PDF**: {pdf_status}\n"
        f"- **POA&M CSV**: {poam_path}\n"
    )
