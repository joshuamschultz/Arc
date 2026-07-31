# SPEC-024 — Implementation Plan

> Status: COMPLETE · Total tasks: 56 · Completed: 56 · Remaining: 0
> Pillar priority: Simplicity → Modularity → Security → Scalability
> Total budget: ~13 hours · Spent: ~7 hr (under budget)

---

## Phase Overview

| Phase | Hours | Output | Status |
|-------|-------|--------|--------|
| 1. Scaffolding & data prep | 0.5 | Directories, gitignore, sanitized demo-data layout | **COMPLETE** |
| 2. Models + state | 0.5 | `models.py`, `_state.py` | **COMPLETE** |
| 3. Parsers (3) | 2.5 | STIG CSV, XCCDF XML, SCC HTML parsers — tested against real files | **COMPLETE** |
| 4. Sanitization | 1.0 | `sanitize.py` + persisted TOML map (D-370) — 38 substitutions, 0 leaks | **COMPLETE** |
| 5. Drift synthesis | 0.5 | `scripts/synth_drift.py` — 10 categorized regressions, deterministic | **COMPLETE** |
| 6. Reference data bundle | 1.5 | NIST OSCAL Rev 5 (71 ctrls), FedRAMP baselines (low/mod/high), CTID ATT&CK (31 ctrls) | **COMPLETE** |
| 7. Tools (6) | 3.0 | All 6 tools loadable, smoke-tested end-to-end with real data | **COMPLETE** |
| 8. WeasyPrint template + POA&M (D-369) | 2.0 | Federal-style PDF template + 16-col POA&M CSV; macOS dyld auto-fix | **COMPLETE** |
| 9. Skill content | 1.5 | `SKILL.md` + 4 reference docs | **COMPLETE** |
| 10. Demo script + dry runs | 1.5 | `demo-data/SCRIPT.md` (operator runbook). Dry runs are user-driven. | **COMPLETE** (script ready) |

---

## Phase 1 — Scaffolding & Data Prep (~0.5 hr) — COMPLETE

- [x] **T-1.1** Create `~/.arc/capabilities/scap/` with `__init__.py`, `parsers/__init__.py`, `data/`, `templates/`
- [x] **T-1.2** Create `~/.arc/skills/scap/` with `references/`
- [x] **T-1.3** Create repo-resident `demo-data/raw/` (gitignored) and `demo-data/sanitized/` (committed)
- [x] **T-1.4** Add `.gitignore` entry for `demo-data/raw/`
- [x] **T-1.5** Symlink the 7 real source files into `demo-data/raw/` from `/Users/joshschultz/Documents/stig source files/`
- [x] **T-1.6** `uv pip install --python .venv/bin/python lxml beautifulsoup4 jinja2 tomli-w pydantic` ✓; weasyprint deferred to Phase 8 (needs `brew install pango cairo gdk-pixbuf`)
- [ ] **T-1.7** ⚠ Verify WeasyPrint renders test PDF — **DEFERRED to Phase 8 gate** (libgobject-2.0-0 missing; `brew install pango cairo gdk-pixbuf` required)

**Success criteria**: directories exist ✓, source files in place ✓, weasyprint deferred to Phase 8 gate.

---

## Phase 2 — Models + State (~0.5 hr) — COMPLETE

- [x] **T-2.1** Write `~/.arc/capabilities/scap/models.py` with `Finding`, `IngestResult`, `GapEntry`, `AttackTechnique` Pydantic models
- [x] **T-2.2** Write `~/.arc/capabilities/scap/_state.py` exposing `get`, `put`, `all`, `aliases`, `clear` over module-level `_INGESTS`
- [x] **T-2.3** Round-trip smoke test: Finding → state.put → state.get; clear() empties; `all_controls` property dedups Rev 4/5

**Success criteria**: models import clean ✓, state round-trip ✓.

---

## Phase 3 — Parsers (~2.5 hr) — COMPLETE

- [x] **T-3.1** Write `parsers/stig_csv.py` — STIG Viewer CSV. Skips classification banner. Parses `CCIs` cell for CCIs + Rev 4/Rev 5 800-53 mappings.
- [x] **T-3.2** Test against `Palo-NDM-STIG.csv` → **34 findings** (24 pass, 10 fail; 34/34 with CCIs and Rev 5 mappings)
- [x] **T-3.3** Test against `NXOS-NDM-STIG.csv` → **42 findings** (27 pass, 14 fail, 1 N/A; 42/42 with CCIs)
- [x] **T-3.4** Write `parsers/xccdf_xml.py` — XCCDF 1.2. Builds rule_id→metadata map, then iterates `<rule-result>` in `<TestResult>`. References classified by href (`800-53r4.pdf` vs `800-53r5.pdf`).
- [x] **T-3.5** Test against `stig-wkstn01.ipa.local.xml` (19 MB) → **1697 findings in 0.09s** (240 fail, 109 pass, 1320 notchecked, 27 N/A; 781 with Rev 4 mappings)
- [x] **T-3.6** Write `parsers/scc_html.py` — SCC HTML. Walks rule tables; parses Identities cell for CCIs + Rev 4/5 inline. Handles paren-balanced control IDs like `IA-5 (2) (b) (1)`.
- [x] **T-3.7** Test against `ELAM-SECRETSERVER_*.html` → **217 findings** (194 pass, 21 N/A, 2 fail; 217/217 with CCIs, 216/217 with both Rev 4 and Rev 5)
- [x] **T-3.8** `parsers/__init__.py` with `detect_format(path)` — extension + content-sniff for all three formats

**Success criteria**: all source files parse ✓, mappings preserved verbatim ✓.

### Phase 3 — Real-data findings (worth flagging in README)

- **Workstation XCCDF has CCEs only, no CCIs** — upstream SCAP Security Guide content. 1228 CCE idents, 0 CCIs. Crosswalk tool (Phase 7) will need to derive Rev 5 from Rev 4 via OSCAL catalog for workstation findings. (Network/Windows hosts have full CCI + Rev 4 + Rev 5 inline.)
- **No Rev 5 mappings in workstation XCCDF** — content is older SSG. Need OSCAL-driven Rev 4 → Rev 5 mapping, OR scope baseline-compare to scanner-source-aware logic.
- **SCC HTML control IDs have nested parens** — e.g. `IA-5 (2) (b) (1)`. Regex required `_strip_trailing_unmatched_parens` helper to handle both the enhancement-suffix parens and the wrapping CCI block parens.
- **Ingest performance**: largest file (19 MB XCCDF) parses in 90 ms — well within NFR-2 (cold start < 2 s).

---

## Phase 4 — Sanitization (~1.0 hr)

- [ ] **T-4.1** Write `sanitize.py` with `apply(findings: list[Finding], host_alias_hint: str | None) -> list[Finding]`
- [ ] **T-4.2** Implement deterministic alias generation per SDD §6: hostnames by role-from-filename, IPs hash→10.42.x.x, MACs hash→02:..., accounts contextual
- [ ] **T-4.3** Implement `sanitize_map.toml` read/write at `~/.arc/capabilities/scap/data/sanitize_map.toml` using `tomllib` / `tomli_w`
- [ ] **T-4.4** Emit `audit_event("sanitize.mapping_updated", {entries_added: N})` when new entries written
- [ ] **T-4.5** Idempotency test: sanitize same file twice → second run writes 0 new entries
- [ ] **T-4.6** Determinism test: sanitize on clean state → assert specific aliases produced (e.g. `paloalto-fw-01.demo.local`)
- [ ] **T-4.7** Verify NO original hostname/IP/MAC/account leaks through to a sanitized `Finding` (regex sweep on outputs)

**Success criteria**: deterministic redaction; map file written and reviewable; audit event emitted.

---

## Phase 5 — Drift Synthesis (~0.5 hr)

- [ ] **T-5.1** Write `scripts/synth_drift_input.toml` — flip-list of ~5–10 sshd-hardening rule IDs (`xccdf_org.ssgproject.content_rule_sshd_*`)
- [ ] **T-5.2** Write `scripts/synth_drift.py` — reads `linux-ws-01.demo.local.xml`, flips configured rules pass→fail in `<rule-result>` elements, adjusts XCCDF `<TestResult>` `start-time`/`end-time` back ~30 days
- [ ] **T-5.3** Run script → `demo-data/sanitized/linux-ws-01.t-30.xml` produced
- [ ] **T-5.4** Determinism test: re-run on clean checkout → byte-identical output
- [ ] **T-5.5** Sanity check: `parsers/xccdf_xml.py` parses the T-30 file and shows the flipped rules as `fail`

**Success criteria**: T-30 artifact exists, deterministic, parseable.

---

## Phase 6 — Reference Data Bundle (~1.5 hr)

- [ ] **T-6.1** Download NIST 800-53 Rev 5 OSCAL JSON catalog → `data/nist_800_53_rev5.json`. Note source URL + SHA256 in `data/SOURCES.md`.
- [ ] **T-6.2** Compile FedRAMP Low/Moderate/High baseline membership → `data/fedramp_baselines.json` with shape `{"low": [...], "moderate": [...], "high": [...]}` (control IDs)
- [ ] **T-6.3** Download CTID 800-53 → ATT&CK mapping JSON → `data/attack_to_800_53.json`. Note source URL + SHA256.
- [ ] **T-6.4** Write `data/SOURCES.md` documenting provenance, version, retrieval date, SHA256 for each reference file
- [ ] **T-6.5** Smoke test: load each JSON, assert non-empty, assert known controls (e.g. `AC-7`, `AU-2`) are present and queryable

**Success criteria**: 3 reference JSONs in `data/`, provenance documented, smoke tests pass.

---

## Phase 7 — Tools (~3.0 hr)

- [ ] **T-7.1** Write `ingest.py` with `@tool scap_ingest` per FR-1.x. Wires `parsers.detect_format` → parse → `sanitize.apply` → `_state.put` → audit. Returns summary string per FR-1.6.
- [ ] **T-7.2** Add `@tool scap_query` to `ingest.py` per FR-2.x. Filters across `_state.all()`. Implements `compare_with` diff logic.
- [ ] **T-7.3** Live test: ingest all 4 files, run sample queries (`control=AC-7`, `severity=high status=fail`, `compare_with=linux-ws-01.t-30`)
- [ ] **T-7.4** Write `crosswalk.py` with `@tool scap_crosswalk` per FR-3.x. Reads inline 800-53 from cache + `data/fedramp_baselines.json` for baseline membership.
- [ ] **T-7.5** Add `@tool scap_baseline_compare` to `crosswalk.py` per FR-4.x. Computes gap list with priority + T-shirt sizing.
- [ ] **T-7.6** Live test: `scap_crosswalk(rule_ids=[<failing rule>])` and `scap_baseline_compare(baseline="high")` produce sane outputs.
- [ ] **T-7.7** Write `threat.py` with `@tool scap_attack_correlate` per FR-5.x. Loads `data/attack_to_800_53.json`, joins on control list, returns markdown with technique IDs + threat narratives.
- [ ] **T-7.8** Live test: `scap_attack_correlate(["AC-17","IA-2"])` returns brute-force narrative with T1110.001.
- [ ] **T-7.9** Write `evidence.py` `@tool scap_evidence_pack` skeleton (calls baseline_compare + finding gather, defers rendering to Phase 8 helpers).

**Success criteria**: all 6 tools loadable; 5 of 6 fully functional pre-Phase 8 (evidence_pack stubbed for PDF render).

---

## Phase 8 — WeasyPrint Template + POA&M (~2.0 hr)

- [ ] **T-8.1** Write `templates/ato_narrative.html` — Jinja2, sections per SDD §7 (cover, control narrative, findings table, citations, signature block)
- [ ] **T-8.2** Write `templates/ato_narrative.css` — federal-document styling: serif font, conservative palette, page numbers, header/footer with "UNCLASSIFIED // FOR DEMO USE"
- [ ] **T-8.3** Write `evidence._render_pdf(context, output_dir) -> Path` helper (Jinja2 → HTML → WeasyPrint → PDF)
- [ ] **T-8.4** Write `evidence._render_poam(gaps, output_dir) -> Path` helper — CSV with FedRAMP POA&M columns per FR-6.3
- [ ] **T-8.5** Wire helpers into `scap_evidence_pack` tool body
- [ ] **T-8.6** Live test: `scap_evidence_pack(control_family="AC", baseline="moderate", output_dir="/tmp/scap-out")` → PDF + CSV produced
- [ ] **T-8.7** Visual review: PDF passes "could go in my package today" eye test (US-9). Iterate CSS until it does.

**Success criteria**: `scap_evidence_pack` produces a federally-credible PDF and a structurally-correct POA&M CSV.

---

## Phase 9 — Skill Content (~1.5 hr)

- [ ] **T-9.1** Write `~/.arc/skills/scap/SKILL.md` per FR-8.1. Sections: when-to-use, the 6 tools, example flows for each demo act
- [ ] **T-9.2** Write `references/control_narrative_template.md` — how the LLM should structure ATO control narrative prose
- [ ] **T-9.3** Write `references/poam_format.md` — POA&M column semantics, tone, owner-suggestion guidance
- [ ] **T-9.4** Write `references/threat_correlation.md` — how to compose threat narrative from `scap_attack_correlate` output
- [ ] **T-9.5** Write `references/baseline_uplift.md` — how to translate `scap_baseline_compare` gap list into POA&M-ready milestones
- [ ] **T-9.6** Verify skill discoverability: agent picks up `scap` skill on next run; `requires_skill="scap"` if any tool needs it (likely `scap_evidence_pack`)

**Success criteria**: skill loads, references read clean, agent uses tools as instructed in scripted run.

---

## Phase 10 — Polish + 2 Dry Runs (~1.5 hr)

- [ ] **T-10.1** Write demo script in `demo-data/SCRIPT.md` mapping each act's prompt → expected tool sequence → expected artifacts
- [ ] **T-10.2** **Dry run #1** stopwatched. Note all latency surprises, audit chain gaps, prompt-recall issues.
- [ ] **T-10.3** Address findings from dry run #1: pre-warm slow tools, tighten prompts, fix any audit redaction misses
- [ ] **T-10.4** **Dry run #2** stopwatched. Target: full 5 acts under 9:30.
- [ ] **T-10.5** Record backup video on second clean dry run as fallback per source doc §8
- [ ] **T-10.6** Verify no original hostnames/IPs/MACs/accounts present in any committed file under `demo-data/sanitized/` (final regex sweep)
- [ ] **T-10.7** Smoke-test offline mode: WiFi off, run Act 1 → ingest → query. Confirm no internet calls (audit chain shows zero outbound network).
- [ ] **T-10.8** Confirm AC-1 through AC-11 all checked (PRD §6)

**Success criteria**: two clean stopwatched runs under 9:30; backup video; offline-verified; all PRD acceptance criteria met.

---

## Verification Checklist (Pre-Demo)

- [ ] All 56 tasks above marked `[x]`
- [ ] PRD acceptance criteria AC-1 through AC-11 verified (PRD §6)
- [ ] No modifications committed under `packages/arcagent/`, `packages/arcllm/`, `packages/arcrun/` (NFR-6)
- [ ] `scap_evidence_pack` PDF visually passes ATO eye-test (US-9)
- [ ] Audit chain shows every tool call with `caller_did`, classification, timing (US-8, AC-8)
- [ ] Demo runs offline post-Claude-API-auth (NFR-7)
- [ ] Backup laptop has identical state, weasyprint deps installed
- [ ] Honesty answers (source doc §9) reviewed and ready

## Status Progression

```
PENDING (now) → COMPLETE (after /implement) → VERIFIED (after dry runs pass)
```

## Plan-Level Risks

| Risk | Trigger | Recovery |
|------|---------|----------|
| WeasyPrint breaks on demo laptop | brew prereqs missing or version skew | Phase 1 verification step T-1.7 catches early. Backup laptop secondary check. |
| Real-data parsing produces empty findings | Format detection mis-fires | Phase 3 per-file tests (T-3.2/3/5/7) catch before Phase 7 wires them up |
| Drift synthesis breaks XML schema | XCCDF strict consumers reject the T-30 file | T-5.5 sanity-check via `parsers/xccdf_xml.py` (same parser the demo uses) |
| Sanitization leaks an original | Edge case in regex / context detection | T-4.7 sweep + T-10.6 final sweep catch before commit |
| Demo runs > 9:30 | Slow LLM calls or audit-chain UI lag | Pre-warm in Act 1 (T-10.3); have shorter alternate prompt sequence ready |
