# SPEC-024 — Product Requirements Document

> Status: draft · Type: integration · Pillar priority: Simplicity → Modularity → Security → Scalability

---

## 1. Background

NLIT 2026 (Kansas City, May 4–7) is the venue. The pitch is to federal IT leaders, ISSOs, ATO authorizers, and CTG Federal partners. The demo must answer one question: **what does Arc add on top of SCAP scanners that the scanners alone cannot?**

OpenSCAP and DISA SCC are point-in-time CLI tools. They emit XML/HTML/CSV and stop. The work that follows — ATO control narratives, POA&M drafting, cross-baseline gap analysis, threat-informed reasoning, multi-host correlation, signed audit chains — is what ISSOs lose sleep over. Arc reasons over scanner output conversationally and produces auditor-grade artifacts in seconds.

Wrapping SCAP positions Arc as **additive to the federally-trusted source-of-truth**, not competitive. Source doc: `/Users/joshschultz/Desktop/arc-openscap-nlit-demo.md`.

## 2. Stakeholders

| Stakeholder | Concern |
|---|---|
| **NLIT audience** (federal IT, ISSOs, agency CIOs) | See real federal data + real reasoning, not chatbot theater |
| **Demo presenter** | Run a 9-minute live demo without flakes; have honesty answers ready for hard questions |
| **Federal compliance** (DOE, NASA, agency authorizers) | Real STIG content, real 800-53 mappings, audit chain visible |
| **Future maintainer (CTG Federal product)** | Extension is the v0 of a productized SCAP capability; clean enough to extend post-NLIT |
| **Source-data privacy** | Hostnames/IPs sanitized, mapping reviewable, original files never shipped |

## 3. User Stories

| ID | As a… | I want to… | So that… |
|---|---|---|---|
| US-1 | demo presenter | ingest 4 real STIG scan files (CSV/XCCDF XML/SCC HTML) into a queryable model | I can demonstrate multi-scanner support live |
| US-2 | demo presenter | have hostnames/IPs/MACs/usernames sanitized at ingest | I can run on real customer data without exposing it |
| US-3 | demo presenter | inspect the sanitization mapping on disk | I can answer the "how do we trust the rebranding" question with file-on-disk evidence |
| US-4 | demo audience (ISSO) | see findings filtered by host, control family, severity, status | I recognize this is the workflow I do today |
| US-5 | demo audience (ISSO) | see Arc generate a multi-page Access Control evidence package against FedRAMP Moderate | I see the artifact I lose sleep over assembled in seconds |
| US-6 | demo audience (ISSO) | see Arc diff current posture against FedRAMP High and draft POA&M milestones | I see Mod→High uplift compress from days to seconds |
| US-7 | demo audience (security architect) | see Arc map a failed control to MITRE ATT&CK techniques | I see threat-informed compliance no other GRC tool does |
| US-8 | demo audience (auditor) | see every tool call land in the arcui audit chain | I trust this is auditor-grade, tamper-evident |
| US-9 | demo presenter | render ATO control narratives as PDFs that look like real federal documents | the artifact passes the "this could go in my package today" eye-test |
| US-10 | demo presenter | drift Act 4 land believably | T-30 fork shows real sshd-hardening regression, not toy data |
| US-11 | post-demo maintainer | extend the extension with `scap_remediate`, `scap_tailor`, live `scap_run` | the architecture supports it without a rewrite |

## 4. Functional Requirements

> Format: EARS. Every FR maps to at least one pillar.

### 4.1 `scap_ingest`

- **FR-1.1** `scap_ingest(path: str, host_alias: str | None = None) -> str` SHALL parse STIG Viewer CSV, OpenSCAP XCCDF XML, and DISA SCC HTML formats and load findings into the module-level ingest cache. (Simplicity, Modularity)
- **FR-1.2** `scap_ingest` SHALL detect format from file extension and content sniff (XCCDF root element, SCC HTML signature, STIG CSV header row). (Simplicity)
- **FR-1.3** `scap_ingest` SHALL sanitize hostnames, FQDNs, IP addresses, MAC addresses, and account names per `sanitize.py` rules **before** any data is cached or returned. (Security — never expose real customer infrastructure)
- **FR-1.4** `scap_ingest` SHALL persist the sanitization mapping to `~/.arc/capabilities/scap/data/sanitize_map.toml` on first ingest, and merge new entries on subsequent ingests. (Security, transparency — D-370)
- **FR-1.5** `scap_ingest` SHALL emit a framework `audit_event()` per call with sensitive-key redaction; audit MUST include source path, host_alias, format detected, finding count. (Security — NIST 800-53 AU-2)
- **FR-1.6** `scap_ingest` SHALL return a human-readable summary string: `"Ingested {N} findings from {path} (host={alias}, format={fmt})."` with line-prefix `Error: ...` on failure. (Convention — D-374)
- **FR-1.7** Each ingested finding SHALL preserve verbatim: rule ID, CCI(s), 800-53 Rev 4 + Rev 5 mappings, severity, fix text, status (`pass`/`fail`/`notchecked`/`notapplicable`). (Credibility — source doc §4 "preserved as-is")

### 4.2 `scap_query`

- **FR-2.1** `scap_query` SHALL filter findings across all ingested hosts by combinations of: host_alias, rule_id (regex), control (e.g. `AC-7`), severity, status, scanner_source. (Simplicity)
- **FR-2.2** `scap_query` SHALL support a `compare_with` parameter naming a second host_alias, returning a diff (rules-changed list with old/new status). (Drift detection — Act 4)
- **FR-2.3** `scap_query` SHALL emit an audit event per call. (Security — AU-2)
- **FR-2.4** Result SHALL be a markdown table fit for direct LLM context use. (Convention — D-374)

### 4.3 `scap_crosswalk`

- **FR-3.1** `scap_crosswalk` SHALL accept a list of rule IDs OR a list of failed-control queries and return mappings: rule → CCI(s) → 800-53 control(s) → FedRAMP baseline membership(s). (Simplicity)
- **FR-3.2** Source data for the crosswalk SHALL be (a) inline mappings already present in STIG CSV / XCCDF XML for rule→CCI→800-53; (b) bundled `data/fedramp_baselines.json` for baseline membership. (Modularity — D-373, D-383)
- **FR-3.3** Result SHALL be a markdown table. Audit event per call. (Convention)

### 4.4 `scap_baseline_compare`

- **FR-4.1** `scap_baseline_compare(baseline: Literal["low","moderate","high"])` SHALL produce a prioritized control-gap list comparing current posture (across all ingested hosts) to the named FedRAMP baseline. (Simplicity)
- **FR-4.2** Output SHALL include: gap controls, rule IDs that fail per gap, severity-weighted priority score, suggested effort tier (T-shirt sizing). (Source doc §6 Act 3)
- **FR-4.3** Audit event per call. (Security — AU-2)

### 4.5 `scap_attack_correlate`

- **FR-5.1** `scap_attack_correlate` SHALL accept a list of failed control IDs and return MITRE ATT&CK techniques those failures expose, with technique IDs (e.g. `T1110.001`), names, tactic, and a short threat-narrative paragraph. (Simplicity, Differentiation)
- **FR-5.2** Mapping data source SHALL be CTID's published 800-53 → ATT&CK JSON, bundled in `data/attack_to_800_53.json`. (D-380)
- **FR-5.3** Result SHALL be markdown with technique IDs as links to attack.mitre.org URLs. (Polish)
- **FR-5.4** Audit event per call. (AU-2)

### 4.6 `scap_evidence_pack`

- **FR-6.1** `scap_evidence_pack(control_family: str, baseline: Literal["low","moderate","high"], output_dir: str)` SHALL render an ATO control narrative as a PDF using WeasyPrint (D-369) and a POA&M as CSV. (Simplicity per pick; Differentiation per artifact)
- **FR-6.2** PDF template SHALL mimic federal ATO document styling: cover page, control-by-control narrative with rule citations, finding tables, signature block placeholder. (Polish — US-9)
- **FR-6.3** POA&M CSV columns SHALL match the standard FedRAMP POA&M template structure: Weakness, Severity, Source, NIST Control(s), Milestones, Status, Remediation language, Owner, Due date. (D-381)
- **FR-6.4** Output paths SHALL be returned in the result string; audit event per call SHALL include the output paths. (AU-2 + traceability)

### 4.7 Drift synthesis (build-time, not runtime)

- **FR-7.1** A `scripts/synth_drift.py` script SHALL read the workstation XCCDF (`linux-ws-01.xml`), flip a configured list of sshd-hardening rules (`xccdf_org.ssgproject.content_rule_sshd_*`) from `pass` → `fail`, adjust XCCDF `<test-result>` start/end timestamps back ~30 days, and write `linux-ws-01.t-30.xml`. (D-371)
- **FR-7.2** The script SHALL be deterministic: same input + same flip-list → byte-identical output. (Reproducibility for re-runs)
- **FR-7.3** The script SHALL run once at build time; the produced T-30 file is checked in alongside the sanitized live file. (Modularity — runtime path is unchanged)

### 4.8 Skill content

- **FR-8.1** `~/.arc/skills/scap/SKILL.md` SHALL describe when and how to use the 6 tools, with example flows for ATO assembly, gap analysis, drift, and threat correlation. (Source doc §2)
- **FR-8.2** `references/` SHALL contain: `control_narrative_template.md`, `poam_format.md`, `threat_correlation.md`, `baseline_uplift.md`. (Source doc §2)

## 5. Non-Functional Requirements

| ID | Requirement | Pillar |
|----|-------------|--------|
| NFR-1 | All 6 tools complete a single call in < 5 s on the demo laptop with all 4 hosts ingested (excludes WeasyPrint render which may take up to 10 s) | Performance |
| NFR-2 | Cold start (first `scap_ingest` after fresh agent run) < 2 s | Performance |
| NFR-3 | Memory footprint with 4 hosts ingested < 200 MB resident | Scalability |
| NFR-4 | All 6 tools classified `read_only`; audit event on every invocation | Security (NIST 800-53 AU-2) |
| NFR-5 | Sanitization mapping is deterministic — same input file → same demo aliases | Security, Reproducibility |
| NFR-6 | Zero modifications to `arcagent` core packages | Modularity |
| NFR-7 | Demo runs offline (no internet required after Claude API auth) | Demo robustness |
| NFR-8 | WeasyPrint output is a single self-contained PDF (no external image refs) | Demo robustness |

## 6. Acceptance Criteria

- [ ] **AC-1** All four source files (Palo CSV, NXOS CSV, RHEL XCCDF XML, Win2019 SCC HTML) ingest successfully via `scap_ingest`. Findings count matches what manual inspection of the source files yields. (FR-1.x)
- [ ] **AC-2** `~/.arc/capabilities/scap/data/sanitize_map.toml` exists after first ingest and contains entries for every real hostname/IP/MAC/account in the source data. (FR-1.4)
- [ ] **AC-3** `scap_query` returns the correct subset for: (a) `host_alias=linux-ws-01.demo.local, control=AC-7`, (b) `severity=high, status=fail`, (c) `compare_with=linux-ws-01.t-30` showing the synthesized drift. (FR-2.x)
- [ ] **AC-4** `scap_crosswalk` for a failing rule returns a chain rule_id → CCI → 800-53 → FedRAMP baseline that matches what the source data carries inline. (FR-3.x)
- [ ] **AC-5** `scap_baseline_compare(baseline="high")` produces a non-trivial gap list with priority-ordered controls and effort estimates. (FR-4.x)
- [ ] **AC-6** `scap_attack_correlate(["AC-17","IA-2"])` returns a sshd / brute-force narrative referencing T1110.001 with a coherent threat paragraph. (FR-5.x)
- [ ] **AC-7** `scap_evidence_pack(control_family="AC", baseline="moderate", ...)` produces a PDF that visually resembles a federal ATO document (cover, control narrative, citations, signature block) plus a POA&M CSV with proper columns. (FR-6.x)
- [ ] **AC-8** Every tool invocation appears in the arcui audit chain with `caller_did`, tool name, classification, and timing. (FR-1.5, NFR-4)
- [ ] **AC-9** `scripts/synth_drift.py` produces `linux-ws-01.t-30.xml` deterministically. Re-running on a clean checkout produces a byte-identical file. (FR-7.x)
- [ ] **AC-10** End-to-end 9-minute demo runs cleanly start to finish twice in a row (two stopwatched dry runs). (Source doc §7)
- [ ] **AC-11** No modifications to `packages/arcagent`, `packages/arcllm`, `packages/arcrun`. (NFR-6)

## 7. Constraints

- **Time**: ~13 hours of build, 1.5 days realistic
- **Demo length**: 9 minutes, 5 acts
- **Scope**: read-only only; no remediation, no live scans, no bundle distribution
- **Real data, rebranded**: hostnames/IPs/MACs/accounts sanitized; rule IDs, CCIs, 800-53 mappings, findings preserved verbatim
- **No internet during demo** except Claude API call

## 8. Dependencies

- `weasyprint` (Python pkg) — D-369 — `pip install weasyprint`
- `lxml` — for XCCDF XML parsing
- `beautifulsoup4` — for SCC HTML parsing fallback
- `tomli` / `tomli-w` (or stdlib `tomllib` on 3.11+) — for sanitize_map.toml read/write
- macOS prereqs: `brew install pango cairo gdk-pixbuf` (documented in extension README, post-NLIT bundle work)

## 9. Open Questions

_(none — see "Honest Gaps to Flag When Asked" in source doc §9 for pitch-time Q&A)_

## 10. References

- Source doc (build-ready plan): `/Users/joshschultz/Desktop/arc-openscap-nlit-demo.md`
- Build decisions: `.claude/decisions-log.md` § "NLIT SCAP Demo — Build Decisions (2026-05-04)"
- Build state: `.claude/builds/nlit-scap-demo/state.json`
- Capability framework: `packages/arcagent/src/arcagent/tools/_decorator.py`
- Built-in tool examples: `packages/arcagent/src/arcagent/builtins/capabilities/{read,find}.py`
- Related: SPEC-021 (capability system), SPEC-023 (arcui chat surface for the demo)
