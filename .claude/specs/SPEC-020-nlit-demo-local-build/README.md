# SPEC-020: NLIT 2026 Demo — Local Arc Agent Build

## Metadata

| Field | Value |
|-------|-------|
| **ID** | SPEC-020 |
| **Feature** | nlit-demo-local-build |
| **Type** | Generic (two new `team/` agents + small `arcui` frontend addition) |
| **Status** | DRAFT |
| **Created** | 2026-04-27 |
| **Author** | Claude Opus 4.7 (planning session) |
| **Priority** | High (NLIT 2026, May 4–7) |
| **Confidence** | 92% (brainstorm + 8 build decisions + 6 deepen research agents + original PRD) |
| **Tier target** | personal (federal-ready as product seed) |
| **Routing** | Fast-track (single review gate) |

## Coding Identity

Per `~/.claude/agents/principled-coder.md`. Pillars in priority order: **Simplicity → Modularity → Security → Scalability**.

This spec is a Pillar 1 + Pillar 2 application: two **independent** Arc agents demonstrating two **independent** modes of "agent as coworker." No shared vault, no shared skills, no cross-agent coordination — each agent is structurally and operationally isolated. The original PRD's "shared brain with auto-discovered cross-link" mechanic was deliberately replaced with this cleaner separation during `/build`.

## Substrate

| Source | Location | Status | Relationship |
|--------|----------|--------|--------------|
| Original PRD | `.claude/NLIT2026-Demo-PRD.md` | reference | Defines the conference talk + two acts. This spec implements a simplified, more honest version. |
| Brainstorm | `.claude/brainstorms/2026-04-27-nlit-demo-local-build.md` | reference | Inspiration, audience, principles, scope. |
| Build decisions | `.claude/decisions-log.md` (search "nlit-demo-local-build") | reference | 8 + 1 decisions + 5 deepen resolutions. |
| Build state | `.claude/builds/nlit-demo-local-build/state.json` | reference | Final design summary. |
| Solutions archive | `.claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md` | reference | Hardening already applied to scheduler we depend on. |
| SPEC-002 | `.claude/specs/SPEC-002-scheduling-heartbeat/` | dependency | Defines the `[modules.scheduler]` we use unmodified. |
| SPEC-015, SPEC-016, SPEC-019 | `.claude/specs/SPEC-01[569]-*` | dependency | Define `arcui` web dashboard we extend with one small card. |

## Adds

1. **`team/nlit_soc_agent/`** — new arc agent project. Conversational SOC threat hunter, served as long-lived process during the live demo. Workspace with 9 entity templates (real STIG XCCDF + NIST 800-53 Rev 5 + POA&M field names). One custom tool: `write_entity` (template-driven). Identity.md with explicit tool-trigger instructions. Three message-format few-shot examples loaded as pre-conversation turns.
2. **`team/nlit_cora_agent/`** — new arc agent project. Autonomous CORA (formerly CCRI) compliance analyst. Served as long-lived process; `[modules.scheduler]` fires `0 4 * * * UTC` (= 23:00 EST) overnight. Four custom tools: `read_document`, `stig_cross_reference`, `poam_validator`, `draft_gap_report`. Identity.md grounded in verified DISA/CORA terminology.
3. **`team/nlit_cora_agent/demo-data/`** — hand-authored test data: `system_inventory.md` (4 systems, real federal field names), `stig_checklist.csv` (with V-220812 planted false closure), `poam_log.csv` (with date mismatch), `org_chart.json`.
4. **`team/nlit_soc_agent/demo-prompts/stage-anchors.md`** — 5–7 STIG/security anchor prompts for live stage use; rehearsed but riffable.
5. **`packages/arcui/` Schedule History card** — small frontend addition. Renders schedule fire history (last 5 fires + next fire) from `schedule:completed` and `schedule:failed` bus events. ~2–3 hrs of HTML/JS work; becomes part of arcui going forward (not demo-only).
6. **Demo runbook** — `team/nlit_soc_agent/REHEARSAL.md` and `team/nlit_cora_agent/REHEARSAL.md`. Five-rehearsal checklist (Baseline T-7d → Variation T-5d → Recovery T-3d → Environment T-1d → Dress AM-of). Captures Anthropic-recommended pre-flight items.

## Packages Affected

| Package | Changes | Approx LOC |
|---------|---------|------------|
| **arcagent** | None (consumed unmodified) | 0 |
| **arcrun** | None (consumed unmodified) | 0 |
| **arcllm** | None (consumed unmodified — temp=0 + few-shots are config, not code) | 0 |
| **arcui** | + Schedule History card (frontend HTML/JS + small backend event handler if needed) | ~150 |
| **team/nlit_soc_agent/** | New project | ~300 (templates + write_entity skill + identity + config + anchor prompts) |
| **team/nlit_cora_agent/** | New project | ~500 (4 skills + identity + config + 4 demo data files + report templates) |
| **team/nlit_cora_agent/demo-data/** | Hand-authored test data | ~200 lines of CSV/MD/JSON |
| **Total new code** | | ~1,250 LOC + ~200 lines data |

**Zero modifications to arcagent, arcrun, arcllm.** All consumption via existing public APIs and config.

## Out of Scope (Deferred)

- `scan_and_connect` 4-pass connection engine (PRD Section 4) — not needed; Obsidian renders graph from inline wikilinks.
- `_pending.md`, `_context.md`, `connected_to[]` PRD vault metadata — unused without inference engine.
- Cross-agent connection ("money moment" from PRD) — replaced by two independent demonstrations.
- Cloud deployment (Azure bicep) — local-first per build decision; cloud is post-demo.
- Custom hosted brain viewer (PRD's `demo/knowledge.html` ambition) — Obsidian IS the viewer.
- Backup screen recordings + offline (Nemotron) fallback — demo runs on real Claude over real internet; 5-rehearsal checklist is the resilience plan.
- Memory/recall skills (`/today`, `/recap`) — defer; arc has `[modules.bio_memory]` for that, post-demo integration.

## Federal-Tier Posture (For Product Afterlife)

This spec ships at `tier = "personal"` for v1. Federal-tier hardening is **structurally compatible** but explicitly deferred:

| Pillar 3 concern | Personal v1 | Federal upgrade path |
|---|---|---|
| Skill signing | Unsigned tools in `tools/` | `arctrust.keypair` + Sigstore verification before load (existing in arcagent) |
| Audit retention | JsonlSink to local file | SignedChainSink + S3/SIEM export (existing in arctrust) |
| Identity | Per-agent DID auto-generated | DID issued by org root + cross-signed (existing in arctrust) |
| LLM provider | Claude API (cloud) | Nemotron / Llama on FIPS-validated on-prem inference |
| Vault | Local `~/nlit-soc-vault/` | Encrypted volume + RBAC on entity types |

No code in this spec assumes personal-tier; tier flips via TOML + extras package per ADR-019.

## Files in This Spec

| File | Purpose |
|------|---------|
| `README.md` | This file. Metadata, substrate, scope. |
| `PRD.md` | Functional and non-functional requirements with acceptance criteria. |
| `SDD.md` | Solution design — module boundaries, data flow, key code paths. |
| `PLAN.md` | Implementation plan — phased tasks with TDD. |
| `REVIEW.md` | (Created post-implementation by `/review`) |

## Decisions Log Reference

All decisions live in `.claude/decisions-log.md` under "## Build Decisions: nlit-demo-local-build" + "## Resolutions from /deepen". This README is a stable index; the decisions log is the source of truth for *why*.

## Next Step

`/implement SPEC-020` — execute the plan with TDD per phase.
