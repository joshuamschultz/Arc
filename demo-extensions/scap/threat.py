"""MITRE ATT&CK correlation tool (SPEC-024 §4.5)."""

from __future__ import annotations

from arcagent.tools._decorator import tool

from . import _data


@tool(
    name="scap_attack_correlate",
    description=(
        "Map a list of failed NIST 800-53 controls to the MITRE ATT&CK techniques those "
        "failures expose. Returns technique IDs, names, tactics, and threat narratives. "
        "Uses CTID 800-53 → ATT&CK mapping data."
    ),
    classification="read_only",
    capability_tags=["compliance_check"],
    when_to_use=(
        "After identifying failing controls (via scap_baseline_compare or scap_query) "
        "to translate compliance gaps into adversary-technique exposure."
    ),
    version="1.0.0",
)
async def scap_attack_correlate(controls: list[str]) -> str:
    """Return ATT&CK techniques exposed by the listed control failures."""
    if not controls:
        return "Error: provide a non-empty list of control IDs."

    # Aggregate techniques across all controls; track which controls bring each
    by_technique: dict[str, dict] = {}
    narratives: list[tuple[str, str]] = []
    seen_narrative: set[str] = set()
    not_mapped: list[str] = []

    for ctl in controls:
        ctl_norm = ctl.replace(" (", "(").strip()
        techs = _data.techniques_for(ctl_norm)
        if not techs:
            not_mapped.append(ctl)
            continue
        for t in techs:
            tid = t["technique_id"]
            entry = by_technique.setdefault(tid, {**t, "controls": []})
            if ctl_norm not in entry["controls"]:
                entry["controls"].append(ctl_norm)
        narrative = _data.threat_narrative_for(ctl_norm)
        if narrative and ctl_norm not in seen_narrative:
            narratives.append((ctl_norm, narrative))
            seen_narrative.add(ctl_norm)

    if not by_technique:
        return (
            f"### Threat correlation\n\nNo ATT&CK mappings found for the {len(controls)} "
            f"control(s) provided ({', '.join(controls)})."
        )

    # Markdown output
    out: list[str] = []
    out.append(f"### Threat correlation: {len(controls)} control(s) → "
               f"{len(by_technique)} ATT&CK technique(s)\n")

    # Techniques table
    rows = []
    for tid, info in sorted(by_technique.items(), key=lambda kv: kv[0]):
        url = f"https://attack.mitre.org/techniques/{tid.replace('.', '/')}"
        rows.append([
            f"[{tid}]({url})",
            info["name"][:50],
            info["tactic"][:40],
            ", ".join(info["controls"][:4]),
        ])
    header = ["Technique", "Name", "Tactic", "From controls"]
    out.append("| " + " | ".join(header) + " |")
    out.append("| " + " | ".join("---" for _ in header) + " |")
    for r in rows:
        cells = [str(c).replace("|", "\\|") for c in r]
        out.append("| " + " | ".join(cells) + " |")

    if narratives:
        out.append("\n#### Threat narrative\n")
        for ctl, n in narratives:
            out.append(f"- **{ctl}** — {n}")

    if not_mapped:
        out.append(f"\n_No ATT&CK mapping in bundled data for: {', '.join(not_mapped)}._")

    return "\n".join(out)
