"""Ingest + query tools for the SCAP extension (SPEC-024 §4.1, §4.2).

Defaults read from ``demo-data/sanitized/`` so the demo runs against
committed, sanitized data. ``scap_ingest`` accepts either a host alias
(e.g. ``linux-ws-01.demo.local`` — auto-resolves to the canonical
sanitized file) or an explicit path.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable

from arcagent.tools._decorator import tool

from . import _state
from .models import Finding, IngestResult
from .parsers import detect_format, scc_html, stig_csv, xccdf_xml
from .sanitize import HOST_ALIASES, output_filename_for

# Default location demo runs against. The SCAP extension is intentionally
# decoupled from any specific repo — but for the NLIT demo we prefix this
# default search path so the agent's bare ``scap_ingest`` works without
# absolute paths.
_DEMO_DIRS = [
    Path.home() / "Projects" / "arc" / "demo-data" / "sanitized",
    Path.cwd() / "demo-data" / "sanitized",
]

# Map host alias → canonical sanitized filename
_ALIAS_TO_FILE = {
    "linux-ws-01.demo.local":  "linux-ws-01.demo.local.xml",
    "linux-ws-01.t-30":         "linux-ws-01.t-30.xml",
    "win2019-app-01.demo.local": "win2019-app-01.demo.local.all-settings.html",
    "paloalto-fw-01.demo.local": "paloalto-fw-01.demo.local.csv",
    "cisco-nxos-01.demo.local":  "cisco-nxos-01.demo.local.csv",
}


def _resolve_path(path_or_alias: str) -> Path | None:
    """Resolve an alias or explicit path to an existing file."""
    p = Path(path_or_alias)
    if p.is_absolute() and p.exists():
        return p
    # Alias lookup
    fname = _ALIAS_TO_FILE.get(path_or_alias)
    if fname:
        for d in _DEMO_DIRS:
            candidate = d / fname
            if candidate.exists():
                return candidate
    # Bare filename search across demo dirs
    for d in _DEMO_DIRS:
        candidate = d / path_or_alias
        if candidate.exists():
            return candidate
    if p.exists():
        return p
    return None


def _alias_from_path(path: Path) -> str:
    """Reverse-lookup a host_alias from a sanitized filename."""
    for alias, fname in _ALIAS_TO_FILE.items():
        if path.name == fname:
            return alias
    return path.stem


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _parse(path: Path, host_alias: str) -> list[Finding]:
    fmt = detect_format(path)
    if fmt == "stig_csv":
        return stig_csv.parse(path, host_alias=host_alias)
    if fmt == "xccdf_xml":
        return xccdf_xml.parse(path, host_alias=host_alias)
    if fmt == "scc_html":
        return scc_html.parse(path, host_alias=host_alias)
    raise ValueError(f"Unsupported format: {fmt}")


@tool(
    name="scap_ingest",
    description=(
        "Parse a STIG CSV, XCCDF XML, or SCC HTML report into a queryable model. "
        "Accepts either a demo host alias (linux-ws-01.demo.local, paloalto-fw-01.demo.local, "
        "cisco-nxos-01.demo.local, win2019-app-01.demo.local, linux-ws-01.t-30) or an absolute path."
    ),
    classification="read_only",
    capability_tags=["compliance_check", "file_read"],
    when_to_use="When loading SCAP scan output for analysis or evidence assembly.",
    version="1.0.0",
)
async def scap_ingest(path: str, host_alias: str | None = None) -> str:
    """Ingest a SCAP source file into the in-memory cache."""
    resolved = _resolve_path(path)
    if resolved is None:
        return f"Error: File not found: {path}. Available aliases: {', '.join(sorted(_ALIAS_TO_FILE))}"
    alias = host_alias or _alias_from_path(resolved)
    try:
        findings = _parse(resolved, alias)
    except ValueError as e:
        return f"Error: {e}"
    except Exception as e:  # noqa: BLE001
        return f"Error: Parse failed for {resolved.name}: {type(e).__name__}: {e}"
    fmt = detect_format(resolved)
    result = IngestResult(
        host_alias=alias,
        scanner_source=fmt,  # type: ignore[arg-type]
        findings=findings,
        ingested_at=_now_iso(),
        source_path=str(resolved),
    )
    _state.put(alias, result)
    fail = sum(1 for f in findings if f.status == "fail")
    return (
        f"Ingested {len(findings)} findings from {resolved.name} "
        f"(host={alias}, format={fmt}, fail={fail})."
    )


# ---------------------------------------------------------------------------
# Query
# ---------------------------------------------------------------------------


def _matches(f: Finding, host_alias: str | None, rule_id: str | None,
             control: str | None, severity: str | None, status: str | None) -> bool:
    if host_alias and f.host_alias != host_alias:
        return False
    if rule_id and not re.search(rule_id, f.rule_id, re.IGNORECASE):
        return False
    if severity and f.severity != severity.lower():
        return False
    if status and f.status != status.lower():
        return False
    if control:
        all_controls = f.nist_800_53_rev5 + f.nist_800_53_rev4
        norm = control.upper()
        if not any(norm in c.upper() for c in all_controls):
            return False
    return True


def _md_table(rows: list[list[str]], header: list[str]) -> str:
    if not rows:
        return "| " + " | ".join(header) + " |\n| " + " | ".join("---" for _ in header) + " |\n| " + " | ".join("(empty)" for _ in header) + " |"
    out = ["| " + " | ".join(header) + " |"]
    out.append("| " + " | ".join("---" for _ in header) + " |")
    for row in rows:
        cells = [str(c).replace("\n", " ").replace("|", "\\|") for c in row]
        out.append("| " + " | ".join(cells) + " |")
    return "\n".join(out)


@tool(
    name="scap_query",
    description=(
        "Query ingested findings by host, rule pattern, control, severity, and status. "
        "Use compare_with to diff two ingested host_aliases (drift detection)."
    ),
    classification="read_only",
    capability_tags=["compliance_check"],
    when_to_use="When filtering findings or detecting drift between two scans.",
    version="1.0.0",
)
async def scap_query(
    host_alias: str | None = None,
    rule_id: str | None = None,
    control: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    compare_with: str | None = None,
    limit: int = 100,
) -> str:
    """Filter / diff findings across the ingest cache."""
    if compare_with:
        if not host_alias:
            return "Error: compare_with requires host_alias to identify the 'current' scan."
        a = _state.get(host_alias)
        b = _state.get(compare_with)
        if a is None:
            return f"Error: Host alias '{host_alias}' not ingested. Known: {_state.aliases()}"
        if b is None:
            return f"Error: Host alias '{compare_with}' not ingested. Known: {_state.aliases()}"
        # Diff: rules present in both, status differs
        a_by = {f.rule_id: f for f in a.findings}
        b_by = {f.rule_id: f for f in b.findings}
        rows = []
        for rid in sorted(set(a_by) & set(b_by)):
            af, bf = a_by[rid], b_by[rid]
            if af.status != bf.status:
                short = af.rule_id.replace("xccdf_org.ssgproject.content_rule_", "")
                rows.append([
                    short,
                    af.severity,
                    f"{bf.status} → {af.status}",
                    ", ".join((af.nist_800_53_rev5 or af.nist_800_53_rev4)[:3]) or "(none)",
                ])
        header = ["Rule", "Severity", "Status drift", "800-53"]
        title = f"### Drift: `{compare_with}` → `{host_alias}` ({len(rows)} regression{'s' if len(rows) != 1 else ''})\n\n"
        return title + _md_table(rows[:limit], header)

    # Plain filter
    sources: Iterable[IngestResult] = (
        [_state.get(host_alias)] if host_alias and _state.get(host_alias) else _state.all()
    )
    matching: list[Finding] = []
    for r in sources:
        if r is None:
            continue
        for f in r.findings:
            if _matches(f, host_alias, rule_id, control, severity, status):
                matching.append(f)
    rows = []
    for f in matching[:limit]:
        short = f.rule_id.replace("xccdf_org.ssgproject.content_rule_", "")
        rows.append([
            short[:60],
            f.host_alias,
            f.severity,
            f.status,
            ", ".join((f.nist_800_53_rev5 or f.nist_800_53_rev4)[:3]) or "(none)",
        ])
    header = ["Rule", "Host", "Severity", "Status", "800-53"]
    summary = (
        f"### scap_query: {len(matching)} match{'es' if len(matching) != 1 else ''}"
        f" (showing {min(len(matching), limit)})\n\n"
    )
    if not _state.all():
        return "Error: No hosts ingested yet. Call scap_ingest first."
    return summary + _md_table(rows, header)
