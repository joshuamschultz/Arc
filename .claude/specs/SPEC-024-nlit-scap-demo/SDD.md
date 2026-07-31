# SPEC-024 — Solution Design Document

> Status: draft · Pillar priority: Simplicity → Modularity → Security → Scalability

---

## 1. Architectural Overview

```
┌────────────────────────────────────────────────────────────────────────────┐
│  ArcAgent (unmodified)                                                     │
│  ┌─────────────────────────────────────────────────────────────────────┐   │
│  │  ToolRegistry  ◄── @tool decorator stamps `_arc_capability_meta`    │   │
│  │      │                                                              │   │
│  │      ▼                                                              │   │
│  │  CapabilityLoader  scans ~/.arc/capabilities/scap/  (precedence #2) │   │
│  └─────────────────────────────────────────────────────────────────────┘   │
└──────────┬─────────────────────────────────────────────────────────────────┘
           │
           ▼
┌──────────────────────────────────────────────────────────────────────────┐
│  ~/.arc/capabilities/scap/  (this spec)                                  │
│                                                                          │
│   ingest.py        scap_ingest, scap_query                              │
│   crosswalk.py     scap_crosswalk, scap_baseline_compare                │
│   threat.py        scap_attack_correlate                                │
│   evidence.py      scap_evidence_pack  ──► WeasyPrint                   │
│                                                                          │
│   parsers/                                                              │
│     stig_csv.py    Palo + NX-OS NDM CSV parser                          │
│     xccdf_xml.py   OpenSCAP + SCC XCCDF parser                          │
│     scc_html.py    SCC HTML fallback                                    │
│                                                                          │
│   sanitize.py      hostname/IP/MAC/account redaction + TOML map         │
│                                                                          │
│   models.py        Finding, IngestResult, GapEntry  (Pydantic)          │
│                                                                          │
│   _state.py        module-level _INGESTS: dict[str, IngestResult]       │
│                                                                          │
│   data/                                                                 │
│     nist_800_53_rev5.json                                               │
│     fedramp_baselines.json                                              │
│     attack_to_800_53.json                                               │
│     sanitize_map.toml          (written at first ingest)                │
│                                                                          │
│   templates/                                                            │
│     ato_narrative.html         (WeasyPrint template, Jinja2)            │
│     ato_narrative.css                                                   │
│                                                                          │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│  ~/.arc/skills/scap/                                                     │
│                                                                          │
│   SKILL.md                                                               │
│   references/                                                            │
│     control_narrative_template.md                                       │
│     poam_format.md                                                      │
│     threat_correlation.md                                               │
│     baseline_uplift.md                                                  │
└──────────────────────────────────────────────────────────────────────────┘

┌──────────────────────────────────────────────────────────────────────────┐
│  Build-time only — repo-resident, not shipped in extension               │
│                                                                          │
│   scripts/synth_drift.py                                                │
│   scripts/synth_drift_input.toml   (flip-list config)                   │
│                                                                          │
│   demo-data/                                                            │
│     raw/    — original files (gitignored, kept private)                 │
│     sanitized/                                                          │
│       paloalto-fw-01.demo.local.csv                                     │
│       cisco-nxos-01.demo.local.csv                                      │
│       linux-ws-01.demo.local.xml                                        │
│       linux-ws-01.t-30.xml         (synthesized drift)                  │
│       win2019-app-01.demo.local.html                                    │
└──────────────────────────────────────────────────────────────────────────┘
```

## 2. Module Boundaries

Hard boundaries — no logic crosses these without an explicit contract.

| Module | Responsibility | May Not |
|--------|----------------|---------|
| `parsers/` | Parse raw scanner output → list of `Finding` objects | Sanitize, cache, audit, render |
| `sanitize.py` | Apply deterministic redaction; persist mapping; emit one audit event on first map write | Parse, cache, render |
| `_state.py` | Hold module-level `_INGESTS` dict; expose `get(alias)`, `put(alias, result)`, `all()` | Sanitize, parse, render, audit |
| `models.py` | Pydantic types: `Finding`, `IngestResult`, `GapEntry`, `AttackTechnique` | Any logic |
| `ingest.py` (tools) | Orchestrate parse → sanitize → cache; query the cache | Render, render PDF |
| `crosswalk.py` (tools) | Read cache + bundled reference data; compute mappings | Parse, render PDF |
| `threat.py` (tools) | Read cache + ATT&CK map; compose narrative | Parse, sanitize |
| `evidence.py` (tools) | Read cache + reference data; render via Jinja2 + WeasyPrint | Parse, sanitize |

The framework provides for free (per `arcagent` conventions): `caller_did` injection, `PolicyPipeline` evaluation, `audit_event()` emission with sensitive-key redaction, per-tool timeout enforcement.

## 3. Data Flow

### 3.1 Ingest

```
scap_ingest(path, host_alias)
  │
  ├─► detect_format(path)               # extension + content sniff
  │
  ├─► parsers.{stig_csv|xccdf_xml|scc_html}.parse(path)
  │     returns: list[Finding]  (raw, original hostnames intact)
  │
  ├─► sanitize.apply(findings, host_alias)
  │     - generates new aliases for any unseen original
  │     - merges into ~/.arc/capabilities/scap/data/sanitize_map.toml
  │     - emits audit event "sanitize.mapping_updated" if new entries written
  │     returns: list[Finding]  (sanitized)
  │
  ├─► _state.put(host_alias, IngestResult(findings, scanner_source, ingested_at))
  │
  └─► audit_event("scap.ingest", {path, host_alias, format, count})
       returns: f"Ingested {N} findings from {path} (host={alias}, format={fmt})."
```

### 3.2 Query

```
scap_query(host_alias?, rule_id?, control?, severity?, status?, compare_with?)
  │
  ├─► findings = _state.all() or _state.get(host_alias)
  │
  ├─► if compare_with:
  │      base = _state.get(compare_with).findings
  │      curr = _state.get(host_alias).findings
  │      diff = compute_diff(base, curr)  # by rule_id, status delta
  │      return markdown_table(diff)
  │
  ├─► apply filters (control regex over inline 800-53 mappings)
  │
  └─► audit_event("scap.query", {filters, result_count})
       returns: markdown table of findings
```

### 3.3 Evidence Pack

```
scap_evidence_pack(control_family, baseline, output_dir)
  │
  ├─► gaps = baseline_compare(baseline)
  │     filter to control_family
  │
  ├─► for each control: gather failing findings + rule citations
  │
  ├─► render_pdf(template="ato_narrative.html", context, output_dir)
  │     Jinja2 → HTML → WeasyPrint → PDF
  │
  ├─► render_poam(gaps, output_dir)
  │     CSV with FedRAMP POA&M columns
  │
  └─► audit_event("scap.evidence_pack", {control_family, baseline, pdf_path, poam_path})
       returns: f"PDF: {pdf_path}\nPOA&M: {poam_path}"
```

## 4. Contracts (Public API the LLM Sees)

> Per arcagent convention, schemas are inferred from typed signatures. Returns are strings (markdown or `Error: ...`).

```python
@tool(name="scap_ingest", classification="read_only", capability_tags=["compliance_check","file_read"], when_to_use="...", version="1.0.0")
async def scap_ingest(path: str, host_alias: str | None = None) -> str: ...

@tool(name="scap_query", classification="read_only", ...)
async def scap_query(
    host_alias: str | None = None,
    rule_id: str | None = None,        # regex
    control: str | None = None,        # e.g. "AC-7"
    severity: str | None = None,       # high|medium|low
    status: str | None = None,         # pass|fail|notchecked|notapplicable
    compare_with: str | None = None,   # second host_alias for drift
    limit: int = 100,
) -> str: ...

@tool(name="scap_crosswalk", classification="read_only", ...)
async def scap_crosswalk(
    rule_ids: list[str] | None = None,
    controls: list[str] | None = None,
    include_baselines: bool = True,
) -> str: ...

@tool(name="scap_baseline_compare", classification="read_only", ...)
async def scap_baseline_compare(
    baseline: Literal["low", "moderate", "high"],
    host_alias: str | None = None,
) -> str: ...

@tool(name="scap_attack_correlate", classification="read_only", ...)
async def scap_attack_correlate(
    controls: list[str],
) -> str: ...

@tool(name="scap_evidence_pack", classification="read_only", ...)
async def scap_evidence_pack(
    control_family: str,                              # e.g. "AC", "AU", "CM", "SC"
    baseline: Literal["low", "moderate", "high"],
    output_dir: str,
) -> str: ...
```

## 5. Internal Types (`models.py`)

```python
from pydantic import BaseModel
from typing import Literal

class Finding(BaseModel):
    rule_id: str
    title: str
    severity: Literal["high", "medium", "low", "informational", "unknown"]
    status: Literal["pass", "fail", "notchecked", "notapplicable", "error"]
    ccis: list[str]
    nist_800_53_rev4: list[str]
    nist_800_53_rev5: list[str]
    fix_text: str | None = None
    discussion: str | None = None
    host_alias: str
    scanner_source: Literal["stig_csv", "xccdf_xml", "scc_html"]

class IngestResult(BaseModel):
    host_alias: str
    scanner_source: str
    findings: list[Finding]
    ingested_at: str  # ISO8601
    source_path: str

class GapEntry(BaseModel):
    control: str
    baseline: Literal["low", "moderate", "high"]
    failing_rules: list[str]
    severity_score: float
    effort_tshirt: Literal["S", "M", "L", "XL"]

class AttackTechnique(BaseModel):
    technique_id: str   # e.g. "T1110.001"
    name: str
    tactic: str
    url: str
    related_controls: list[str]
    threat_narrative: str
```

## 6. Sanitization Algorithm

Deterministic. Same input → same output across runs.

1. **Hostnames / FQDNs**: extract via regex; map by role detected from filename (`palo*` → `paloalto-fw-{n}.demo.local`, `nxos*` → `cisco-nxos-{n}.demo.local`, `wkstn*` → `linux-ws-{n}.demo.local`, `*win*` → `win2019-app-{n}.demo.local`). Counter persists in `sanitize_map.toml`.
2. **IP addresses**: any IPv4/IPv6 in scan output → `10.42.{seed}.{seed}` where `seed = hash(original) % 200 + 10`. Idempotent.
3. **MAC addresses**: replace with `02:00:00:{a}:{b}:{c}` (locally-administered range). Last 3 octets from hash.
4. **Usernames**: detect via context (e.g. `<account>...</account>`, `User: ...`); replace with `demo-user-{n}`.
5. **Preserved verbatim**: rule IDs, CCIs, NIST 800-53 mappings, findings text, fix text, severity, status, scanner version, timestamps.
6. On first encounter of any original token, write its mapping to `data/sanitize_map.toml` and emit `audit_event("sanitize.mapping_updated", {entries_added: N})`.

## 7. WeasyPrint ATO Template

`templates/ato_narrative.html` (Jinja2):

```
[Cover page] CONTROL FAMILY · BASELINE · DATE · SYSTEM (demo placeholder)
[For each control in family]
  ─ Control ID + title (from NIST OSCAL)
  ─ Implementation narrative (LLM-generated, citation-rich)
  ─ Findings table:
       Rule ID | Severity | Status | Hosts affected
  ─ Citations: rule IDs + 800-53 mapping chain
[Signature block placeholder]
```

`templates/ato_narrative.css`: federal-document styling — Times-like serif, conservative color palette, page numbers, header/footer with classification placeholder ("UNCLASSIFIED // FOR DEMO USE").

## 8. Error Handling

Per arcagent convention: tools return `Error: ...` strings, never raise. Error categories:

| Condition | Response |
|-----------|----------|
| File not found | `Error: File not found: {path}` |
| Format detection fails | `Error: Could not determine format for {path}. Supported: STIG CSV, XCCDF XML, SCC HTML.` |
| Parse error | `Error: Parse failed at {location}: {reason}` |
| Unknown host_alias in query | `Error: Host alias '{alias}' not ingested. Known: {list}` |
| WeasyPrint missing system deps | `Error: WeasyPrint cannot render — missing system dependency. macOS: brew install pango cairo gdk-pixbuf.` |
| Empty result set | Returns empty markdown table with header — not an error |

## 9. Audit Events Emitted

| Event | When | Payload (post-redaction) |
|-------|------|--------------------------|
| `scap.ingest` | Every `scap_ingest` call | `{path, host_alias, format, finding_count}` |
| `sanitize.mapping_updated` | When new entries added to `sanitize_map.toml` | `{entries_added}` (no original values in payload) |
| `scap.query` | Every `scap_query` call | `{filters, result_count}` |
| `scap.crosswalk` | Every call | `{rule_count, control_count}` |
| `scap.baseline_compare` | Every call | `{baseline, gap_count}` |
| `scap.attack_correlate` | Every call | `{control_count, technique_count}` |
| `scap.evidence_pack` | Every call | `{control_family, baseline, pdf_path, poam_path}` |

## 10. Tier Policy (Honored, Not Demonstrated in Demo)

| Tier | Behavior for SCAP extension |
|------|------------------------------|
| Federal | Would block load of unsigned bundle (post-NLIT § 11.3 work). For dev-mode files in `~/.arc/capabilities/`: AST-validated per existing capability loader rules. |
| Enterprise | Warns on missing signature; loads with audit warn. |
| Personal | Info-only on missing signature; loads. |
| **Demo (this spec)** | Dev-mode install — files dropped directly. No bundle, no signing. Source doc §10. |

The 6 tools themselves carry no tier-conditional behavior — same code at every tier. Read-only classification means no `PolicyPipeline` deny path is exercised by these tools; framework still evaluates and audits.

## 11. Threat Surface Considerations (per CLAUDE.md)

| Threat | Mitigation in this design |
|--------|---------------------------|
| LLM02 Sensitive Information Disclosure | Sanitization at ingest before any data reaches LLM context. Audit redaction on payloads. |
| LLM05 Improper Output Handling | Tool outputs are LLM-context strings, not eval'd. PDF rendering is template-driven, no LLM-authored HTML executed. |
| LLM06 Excessive Agency | All 6 tools `read_only`. No remediation, no shell, no network. |
| ASI02 Tool Misuse | `capability_tags=["compliance_check","file_read"]` — narrow, allowlistable. |
| ASI04 Agentic Supply Chain | Reference data (NIST OSCAL, FedRAMP, CTID ATT&CK map) is fetched at build time from canonical sources, hashed, checked in. No runtime fetch. |
| ASI06 Memory & Context Poisoning | `_INGESTS` cache is in-process only; no persistence path the LLM can write through. |

## 12. Performance Notes

- Largest input: `stig-wkstn01.ipa.local.xml` at 19 MB. `lxml.etree.parse` handles in < 2 s on demo hardware.
- `_INGESTS` is a dict; lookups O(1). Filter operations are O(n) over total findings (~5–10k across 4 hosts). Acceptable for demo.
- WeasyPrint render of a multi-control ATO PDF: ~5–10 s; pre-warm during Act 1 if needed.

## 13. Out of Scope (Reaffirmed)

- Live `scap_run`, `scap_remediate`, `scap_tailor`
- Bundle distribution, Sigstore, TOFU
- Multi-process / multi-instance scale
- "Act 0" install moment in the pitch

## 14. Open Design Questions

_(none — resolved by `/build`)_
