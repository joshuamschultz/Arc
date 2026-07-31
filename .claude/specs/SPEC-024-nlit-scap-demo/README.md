---
spec_id: SPEC-024
name: nlit-scap-demo
status: draft
type: integration
created: 2026-05-04
intake_confidence: 0.95
type_confidence: 0.75
fast_track: true
prior_work:
  - /Users/joshschultz/Desktop/arc-openscap-nlit-demo.md (source doc — doubles as brainstorm + build plan)
  - .claude/builds/nlit-scap-demo/state.json (build decisions D-369..D-383)
  - .claude/decisions-log.md (NLIT SCAP Demo — Build Decisions, 2026-05-04)
related_specs:
  - SPEC-021-unified-capability-system (capability/extension loader the demo plugs into)
  - SPEC-023-arcui-web-platform-adapter (the chat surface where the demo runs)
  - SPEC-020-nlit-demo-local-build (prior NLIT demo — Atlas Brain; superseded by this scope)
trigger: NLIT 2026 pitch demo (Kansas City, May 4–7). 9-min, 5-act demonstration of Arc reasoning over real federal STIG scan data for ATO evidence assembly, baseline gap analysis, drift detection, and MITRE ATT&CK threat correlation.
pillars_priority: [Simplicity, Modularity, Security, Scalability]
---

# SPEC-024 — NLIT SCAP Demo (Arc + OpenSCAP/SCC Extension)

## TL;DR

Ship a **read-only SCAP capability extension** that lets an Arc agent reason conversationally over real federal STIG scan output. Files drop directly into `~/.arc/capabilities/scap/` and `~/.arc/skills/scap/` (dev-mode install — bundle/signing deferred to post-NLIT per source doc §11). Six `@tool` functions parse OpenSCAP XCCDF XML, SCC HTML, and STIG Viewer CSV; sanitize hostnames/IPs/MACs at ingest with a reviewable persisted mapping; cross-walk findings to NIST 800-53 / FedRAMP / MITRE ATT&CK; and assemble ATO control narratives + POA&Ms via WeasyPrint.

Real data from four hosts: Palo Alto NDM, Cisco NX-OS NDM, RHEL workstation (OpenSCAP), Windows Server 2019 (SCC 5.14). Hostnames rebranded to `*.demo.local`; rule IDs, CCIs, 800-53 mappings, findings preserved verbatim — that's the credibility.

**Build budget**: ~13 hours. **Demo length**: ~9 minutes. **Fast-track**: yes — source doc is build-ready, build decisions logged.

## Approach Summary

```
Source files (4)                        ~/.arc/capabilities/scap/
─────────────────                       ─────────────────────────
Palo-NDM-STIG.csv ──┐                   parsers/stig_csv.py ──┐
NXOS-NDM-STIG.csv ──┼─ scap_ingest ─►   parsers/xccdf_xml.py ─┼─► sanitize.py ─► _INGESTS dict
stig-wkstn01.xml ───┤                   parsers/scc_html.py ──┘            (module-level)
ELAM-...html ───────┘                                                            │
                                                                                 ▼
                                                                  scap_query
                                                                  scap_crosswalk
                                                                  scap_baseline_compare
                                                                  scap_attack_correlate
                                                                  scap_evidence_pack ──► WeasyPrint PDF + POA&M CSV
```

## Decisions (from `/build`)

See `.claude/decisions-log.md` § "NLIT SCAP Demo — Build Decisions (2026-05-04)" for full table. Summary:

| ID | Decision | Choice |
|----|----------|--------|
| D-369 | PDF rendering | **WeasyPrint** (HTML/CSS → PDF) |
| D-370 | Sanitize map persistence | **TOML** at `data/sanitize_map.toml` |
| D-371 | Drift artifact production | **Programmatic generator** at `scripts/synth_drift.py` |
| D-372 | Install location | `~/.arc/capabilities/scap/` (dev-mode, no bundle) |
| D-373..383 | Conventions / mandates | `@tool` decorator, string returns, `read_only` only, audit on every call, deterministic sanitization, module-level cache, OSCAL/FedRAMP/CTID reference data |

## Out of Scope (Explicit)

- Live `scap_run` (invoking openscap-scanner / scc binaries on host)
- `scap_remediate` (state-modifying — needs sandbox + policy + approval flow)
- `scap_tailor` (XCCDF profile authoring)
- Bundle distribution: `arc ext install`, Sigstore sidecar, TOFU approval (post-NLIT, source doc §11)
- "Act 0" install moment in pitch (only after distribution lands)

## Pillars Mapping

| Pillar | How this spec respects it |
|--------|---------------------------|
| **Simplicity** | 6 tools, no clever abstractions, parsers are linear. Module-level cache mirrors built-in `_runtime` pattern. No new framework primitives. |
| **Modularity** | Extension is fully self-contained under `~/.arc/capabilities/scap/`. Skill at `~/.arc/skills/scap/`. Zero modifications to `arcagent` core. |
| **Security** | Read-only classification on all 6 tools. Audit on every call (NIST 800-53 AU-2). Sanitization mapping reviewable on disk + signed audit event. Tier policy honored even though demo runs dev-mode. |
| **Scalability** | Demo-scale (4 hosts, 9 min). Architecture supports horizontal scale via stateless tools — but not exercised here. Cache is per-process for demo simplicity. |

## Files in This Spec

- `README.md` — this file
- `PRD.md` — requirements (EARS format), user stories, acceptance criteria
- `SDD.md` — design, module boundaries, contracts, data flow
- `PLAN.md` — phased implementation tasks

## Decision Log (this spec)

Updated during implementation with discoveries, deviations, gotchas.

_(populated by `/implement` and `/review`)_

## Learnings

_(populated by `/review` after implementation)_

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| WeasyPrint system deps missing on demo laptop | `brew install pango cairo gdk-pixbuf` documented; backup laptop pre-installed |
| Live demo crashes mid-run | Recorded backup video, narrated over if needed (source doc §8) |
| Slow tool call mid-demo | Pre-warm slow tools during Act 1 |
| Audience asks about signing | §9 honesty answers ready (audit emission today, end-to-end SignedChainSink on roadmap) |
| Audience challenges rebranding fidelity | `data/sanitize_map.toml` is reviewable; sanitization is deterministic code |
| Drift artifact looks fake | `scripts/synth_drift.py` uses real-data input; only flips ~5–10 sshd-related rules with realistic config-regression cluster |

## Status

- [x] Build decisions complete (`/build` 2026-05-04)
- [x] Spec created
- [ ] Implementation (`/implement SPEC-024`)
- [ ] Two dry runs stopwatched
- [ ] Demo at NLIT
- [ ] Post-demo review (`/review SPEC-024`)
- [ ] Compound learnings (`/compound`)
