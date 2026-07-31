---
topic: NLIT 2026 Demo — Local Arc Agent Build (Atlas Brain + Obsidian Viewer)
date: 2026-04-27
status: complete
related_prd: .claude/NLIT2026-Demo-PRD.md
---

# NLIT 2026 Demo — Local Arc Agent Build

## Inspiration

Prove the "department brain" concept on stage at NLIT 2026 (Kansas City, May 4-7). Move the narrative from chatbots to coworkers: an agent that doesn't just answer questions but accumulates institutional memory across every workflow it touches. The two-act structure (live CCRI run + overnight SOC walkthrough) culminates in a single emergent moment — a SOC finding auto-connecting to a CCRI finding written hours earlier by a different agent — because they share one brain.

Pivot from the originally-scoped cloud deployment to a **local-first build**: prove the Arc agent framework actually works end-to-end on a laptop before adding cloud complexity. Cloud comes after local works.

## Projects

- **Local Arc agent project** — sibling to `packages/`, *consumes* arc packages without modifying them. Houses identities, skills, pipelines, config, and test data.
- **Atlas vault** — local Obsidian vault at `~/nlit-demo-vault/` (per PRD). Two zones: permanent `Atlas/Entities/` brain + transient `Workflows/` work product.
- **Obsidian as viewer** — leverage Obsidian's built-in graph view as the audience-facing brain visualization. No custom web UI for v1.
- **Two agent identities** — `ccri_compliance_analyst` (live, Act 1) and `soc_threat_hunter` (overnight, Act 2). Same Atlas vault. Cross-agent connection is the money moment.

## Audience

**Stage audience (NLIT 2026):** Federal IT leaders, compliance/security practitioners, agency CIOs evaluating AI for operational work. Skeptical of chatbot demos. Want to see real agent behavior, not scripted theater.

**Downstream audience (product seed):** Federal agencies and federal contractors who need an institutional knowledge base that grows automatically. CTG Federal could productize this. Demo code becomes the v0 foundation.

## Use Cases

- **Live compliance analysis** — Agent ingests STIG checklist, POA&M log, system inventory; surfaces a planted false-closure (V-220812) by reasoning about remediation date vs reimaging date; writes findings into Atlas with linked entities.
- **Overnight SOC triage** — Agent processes 100 SIEM alerts → triages to P1/P2 → correlates host-alpha auth failures with host-beta DNS anomaly (lateral movement) → writes incident ticket and morning handoff.
- **The money moment** — Different agents, hours apart, write into the same Atlas. `host-beta` (SOC) auto-connects to `System-A-RHEL9` (CCRI) via shared subnet. Audience sees the edge appear in Obsidian graph view. Nobody programmed that connection.
- **Architectural extension (post-demo)** — Same vault, same scan_and_connect engine, future workflows: provisioning, change management, incident retrospectives. The brain grows across every workflow.

## Desired Outcomes

- Audience walks away saying "that's a coworker, not a chatbot."
- Demo runs on real Arc agent infrastructure — real arcllm calls to Claude, real scan_and_connect logic, real LLM reasoning. No videos, no scripts, no fakes.
- Local build proves the pattern. Cloud deployment becomes a follow-on, not a prerequisite.
- Code is clean enough that it becomes the canonical Arc agent example and the seed of an actual product.
- Cross-agent Atlas edge (`host-beta ↔ System-A`) visible in Obsidian graph view by end of Act 2.

## Guiding Principles

- **Showcase Arc's actual capability** — strictest principle. Real agent infrastructure on stage. No fake video, no bypass scripts. The agent the audience watches is doing the work the audience sees. Failures are visible — build for resilience.
- **Local-first** — laptop demo. Vault on disk. Obsidian as viewer. Internet only for the Claude API call. Cloud is post-demo.
- **Outside packages** — consume `arcagent`, `arcrun`, `arcllm` as libraries. Don't modify package source unless something is genuinely broken. Customize through agent-level code (identities, skills, pipelines, config).
- **Product-quality code** — this is the seed of a product. Clean separation, real config, no demo-only hacks bleeding into the agent code. Build it the way the next person extending it would want.
- **AI-paced timeline** — user builds quickly with AI assistance. Don't optimize the brainstorm for project management; optimize for technical clarity.

## Constraints

- **Must use existing Arc packages unmodified.** Adjustments to packages only if something is genuinely broken — and even then, the bar is high.
- **Must run locally** on the demo machine (laptop). No cloud dependency for demo execution.
- **Must use Claude via arcllm** (per PRD: claude-sonnet-4-5 with ollama/nemotron fallback).
- **Vault must be Obsidian-compatible** — markdown files, YAML frontmatter, `[[wikilinks]]`, native graph view works without custom plugins (vanilla Obsidian assumed unless decided otherwise in /build).
- **Two distinct agent identities required** — cross-agent Atlas connection is the demo's emotional payoff and only works if there are genuinely two agents writing into the same brain.

## Scope

**In:**
- Atlas vault structure + `_context.md` + `_pending.md`
- All test data with planted evidence (5 PRD files: system inventory, STIG checklist, POA&M log, SIEM alerts, asset criticality)
- Graph skills: `write_obsidian_note`, `write_relationship`, `write_entity`, `scan_and_connect` Passes 1-3
- Memory skills: `memory_recall`, `log_session_event`
- CCRI skills: `read_document`, `stig_cross_reference`, `poam_validator`, `draft_gap_report`, `assign_finding_owners`
- SOC skills: `triage_alerts`, `correlate_events`, `draft_incident_ticket`, `write_morning_handoff`
- Two identity files (CCRI analyst + SOC hunter)
- Two pipelines (ccri_status_check.yaml + soc_triage_pipeline.yaml)
- End-to-end live runs of both pipelines
- Cross-agent connection (`host-beta ↔ System-A`) visible in Obsidian

**Out (deferred):**
- **Pass 4 — proximity scan.** Conversational-proximity edges in scan_and_connect. Most exotic part of the model. Ship Passes 1-3 first; add Pass 4 only if v1 lands fast and there's still time.
- **Backup recordings + offline mode.** No screen-recording fallback, no Nemotron local fallback wiring for v1. Build assumes internet + Claude API works on stage. Resilience comes after the demo runs cleanly.
- Cloud deployment (Azure bicep, hosted brain viewer) — explicitly deferred to post-NLIT.
- Custom web UI (`demo/knowledge.html`) as the viewer — Obsidian native graph view is the v1 viewer.

## Open Questions for /build

- **Project location:** `arc/nlit-demo/` as sibling to `packages/`? Or new repo `arc-nlit-demo/`? Or `arc/examples/nlit-demo/`? Affects how it's installed and shipped as the canonical example.
- **Agent process model:** One arcagent process that swaps identities between pipelines? Two parallel arcagent processes (one per identity) sharing the vault? Affects whether cross-agent isolation is real.
- **Skill packaging:** Skills as Python files registered via `[tools.native]` in arcagent.toml? Or skills as MCP server? Or skills as a separate Python package the agent imports? PRD writes them as `skills/*.py` — likely native tools.
- **Pipeline runner:** PRD describes pipelines as YAML (`pipelines/*.yaml`). Does Arc have a pipeline DSL/runner today, or do we orchestrate via arcrun's ReAct loop with a higher-level controller script? Need to verify against arcrun capability.
- **Vault writes & scan_and_connect performance:** Frontmatter scan across hundreds of notes mid-stage must complete fast. Need to verify or set perf budget.
- **Live reasoning visibility:** How does the audience watch the agent reason? Terminal output? `arctui`? `arcui` web bridge? The "watch the agent think" UX is part of the demo even if the brain viewer is Obsidian.
- **LLM determinism:** Real Claude calls on stage means non-deterministic reasoning. How do we guard against the agent missing the false-closure or correlations? Prompt engineering quality is the lever.
- **Test data location & format:** Generate fresh, or hand-author the 5 files matching PRD specs (planted V-220812, planted host-alpha/beta correlation)?

## Related Solutions

- None in `.claude/solutions/` — no prior work on Atlas/brain/vault patterns.
- Existing brainstorms: agent-messaging, recursive-spawning, arcteam-messaging, arcllm-call-queue, convention-driven-prompt-injection. None directly applicable but `arcteam-messaging` may inform if we go two-process.
