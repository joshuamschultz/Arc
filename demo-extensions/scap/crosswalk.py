"""Crosswalk + baseline-compare tools (SPEC-024 §4.3, §4.4)."""

from __future__ import annotations

from collections import Counter, defaultdict

from arcagent.tools._decorator import tool

from . import _data, _state
from .models import Finding


def _md_table(rows: list[list[str]], header: list[str]) -> str:
    if not rows:
        return "| " + " | ".join(header) + " |\n| " + " | ".join("---" for _ in header) + " |\n| " + " | ".join("(empty)" for _ in header) + " |"
    out = ["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |"]
    for r in rows:
        cells = [str(c).replace("\n", " ").replace("|", "\\|") for c in r]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


@tool(
    name="scap_crosswalk",
    description=(
        "Map rule IDs to CCIs and NIST 800-53 controls, with FedRAMP baseline membership. "
        "Pulls inline mappings already present in source data (Rev 4 + Rev 5)."
    ),
    classification="read_only",
    capability_tags=["compliance_check"],
    when_to_use="When the agent needs to translate rule IDs to control language for ATO narratives.",
    version="1.0.0",
)
async def scap_crosswalk(
    rule_ids: list[str] | None = None,
    controls: list[str] | None = None,
    include_baselines: bool = True,
) -> str:
    """Cross-reference rules ↔ CCIs ↔ 800-53 ↔ FedRAMP baselines."""
    all_findings: list[Finding] = []
    for r in _state.all():
        all_findings.extend(r.findings)
    if not all_findings:
        return "Error: No hosts ingested yet. Call scap_ingest first."

    matched: list[Finding] = []
    if rule_ids:
        wanted = set(rule_ids)
        for f in all_findings:
            short = f.rule_id.replace("xccdf_org.ssgproject.content_rule_", "")
            if f.rule_id in wanted or short in wanted or any(rid in f.rule_id for rid in wanted):
                matched.append(f)
    if controls:
        wanted_c = {c.upper() for c in controls}
        for f in all_findings:
            allc = (f.nist_800_53_rev5 or []) + (f.nist_800_53_rev4 or [])
            if any(any(w in c.upper() for c in allc) for w in wanted_c):
                if f not in matched:
                    matched.append(f)
    if not rule_ids and not controls:
        # Default: show all currently-failing controls
        matched = [f for f in all_findings if f.status == "fail"]

    rows = []
    for f in matched[:60]:
        short = f.rule_id.replace("xccdf_org.ssgproject.content_rule_", "")[:60]
        ctls = f.nist_800_53_rev5 or f.nist_800_53_rev4
        baselines = []
        if include_baselines:
            for ctl in ctls[:5]:
                tags = []
                for b in ("low", "moderate", "high"):
                    if _data.in_baseline(ctl, b):
                        tags.append(b[0].upper())
                baselines.append(f"{ctl}{'(' + ''.join(tags) + ')' if tags else ''}")
        else:
            baselines = ctls[:5]
        rows.append([
            short,
            f.host_alias,
            ", ".join(f.ccis[:3]) or "(none)",
            ", ".join(baselines) or "(none)",
            f.status,
        ])
    header = ["Rule", "Host", "CCIs", "800-53 (baseline tags: L/M/H)", "Status"]
    return (
        f"### Crosswalk: {len(matched)} finding{'s' if len(matched) != 1 else ''} "
        f"(showing {min(len(matched), 60)})\n\n"
        + _md_table(rows, header)
        + "\n\n_Tag legend: L=Low, M=Moderate, H=High FedRAMP baseline membership._"
    )


# ---------------------------------------------------------------------------
# Baseline compare
# ---------------------------------------------------------------------------


def _severity_score(sev: str) -> float:
    return {"high": 3.0, "medium": 2.0, "low": 1.0}.get(sev, 0.5)


def _tshirt_for(rule_count: int, max_sev: str) -> str:
    weight = rule_count + (5 if max_sev == "high" else 2 if max_sev == "medium" else 0)
    if weight <= 2:
        return "S"
    if weight <= 5:
        return "M"
    if weight <= 10:
        return "L"
    return "XL"


@tool(
    name="scap_baseline_compare",
    description=(
        "Compare current posture across all ingested hosts against a FedRAMP baseline "
        "(low/moderate/high). Returns a prioritized control-gap list with severity-weighted "
        "scores and effort estimates."
    ),
    classification="read_only",
    capability_tags=["compliance_check"],
    when_to_use="For Mod→High uplift gap analysis and POA&M drafting.",
    version="1.0.0",
)
async def scap_baseline_compare(
    baseline: str,  # "low" | "moderate" | "high"
    host_alias: str | None = None,
) -> str:
    """Compute control-gap list against a FedRAMP baseline."""
    baseline = baseline.lower()
    if baseline not in ("low", "moderate", "high"):
        return f"Error: baseline must be one of low|moderate|high (got {baseline!r})"
    baselines = _data.fedramp_baselines()
    target = set(baselines.get(baseline, []))
    if not target:
        return f"Error: baseline {baseline!r} has no controls bundled."

    sources = (
        [_state.get(host_alias)] if host_alias else _state.all()
    )
    if any(s is None for s in sources):
        return f"Error: Host alias '{host_alias}' not ingested. Known: {_state.aliases()}"
    if not sources:
        return "Error: No hosts ingested yet. Call scap_ingest first."

    # For each control in baseline, count failing rules that map to it
    fails_by_control: dict[str, list[Finding]] = defaultdict(list)
    pass_set: set[str] = set()
    for r in sources:
        if r is None:
            continue
        for f in r.findings:
            ctls = f.nist_800_53_rev5 or f.nist_800_53_rev4
            for ctl in ctls:
                # Normalize whitespace, e.g. "AC-7 (a)" vs "AC-7(a)"
                norm = ctl.replace(" (", "(").strip()
                if f.status == "fail":
                    fails_by_control[norm].append(f)
                elif f.status == "pass":
                    pass_set.add(norm)

    # Gaps: controls in baseline that have at least one failing rule on the fleet
    gap_rows = []
    for ctl in sorted(target):
        fails = fails_by_control.get(ctl, []) + fails_by_control.get(ctl.replace("(", " ("), [])
        if not fails:
            continue
        sevs = [f.severity for f in fails]
        max_sev = "high" if "high" in sevs else "medium" if "medium" in sevs else "low"
        score = sum(_severity_score(s) for s in sevs)
        tshirt = _tshirt_for(len(fails), max_sev)
        title = _data.control_title(ctl) or "(title unavailable)"
        rules_short = sorted({
            f.rule_id.replace("xccdf_org.ssgproject.content_rule_", "")
            for f in fails
        })[:3]
        gap_rows.append({
            "control": ctl,
            "title": title,
            "fail_count": len(fails),
            "max_severity": max_sev,
            "score": score,
            "tshirt": tshirt,
            "sample_rules": ", ".join(rules_short),
        })

    gap_rows.sort(key=lambda x: -x["score"])
    rows = [[
        r["control"],
        r["title"][:40],
        r["fail_count"],
        r["max_severity"],
        f"{r['score']:.1f}",
        r["tshirt"],
        r["sample_rules"][:60],
    ] for r in gap_rows[:30]]
    header = ["Control", "Title", "# Fails", "Max Sev", "Score", "Effort", "Sample rules"]
    summary = (
        f"### Baseline gap: FedRAMP **{baseline.upper()}** "
        f"({len(gap_rows)} controls with failures)"
    )
    if host_alias:
        summary += f"  scope=`{host_alias}`"
    summary += "\n\n"
    summary += _md_table(rows, header)
    summary += (
        f"\n\n_Effort: S=quick fix, M=moderate, L=multi-step, XL=architectural._\n"
        f"_Total fleet failures contributing to gap: "
        f"{sum(r['fail_count'] for r in gap_rows)}._"
    )
    return summary
