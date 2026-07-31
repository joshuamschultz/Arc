# SPEC-020 — PRD: NLIT 2026 Demo (Local Arc Agent Build)

## 1. Purpose

Prove that the Arc agent framework — installed and running locally — can deliver two distinct modes of "agent as coworker" on stage at NLIT 2026 (May 4–7, Kansas City), in front of a federal IT and security audience.

Two **independent** Arc agents demonstrate two **independent** capabilities:

- **Agent 1 (`nlit_soc_agent`)** — conversational, live on stage. I chat about STIG findings, alerts, and incidents; the agent extracts entities and writes them as richly-structured markdown into an Obsidian-readable workspace. Audience watches the brain populate in real time.
- **Agent 2 (`nlit_cora_agent`)** — autonomous, scheduled. Served before the talk, fires overnight via the built-in scheduler, processes preset CORA (formerly CCRI) compliance data, leaves a gap report. Audience sees the cron history + the report waiting for them.

Together they answer the talk's central question: *can an agent actually be a coworker?* — once as a conversational partner, once as an autonomous worker.

## 2. Audience & Success

**Stage audience:** Federal IT/security leaders skeptical of chatbot demos. Win = "that's a coworker, not a chatbot."

**Downstream audience:** Federal agencies / contractors evaluating Arc as the foundation of an institutional brain product. Win = code is clean enough to be the canonical example others extend.

## 3. Functional Requirements

### FR-1 — Agent 1: Conversational entity extraction

**As** the presenter chatting on stage with `nlit_soc_agent`,
**I want** the agent to reliably call `write_entity` whenever I mention an IT/security entity by name,
**so that** the audience watches richly-structured markdown files appear in Obsidian as I speak.

**Acceptance criteria:**
- AC-1.1 Agent calls `write_entity` on ≥5 of 6 rehearsed entity-mention turns (Anthropic guidance: temp=0 + 3 message-format few-shots + tool description with explicit trigger). [Pillar 1: simple, predictable]
- AC-1.2 Each `write_entity` call selects the correct entity type (System, Event, STIG-Reference, Incident, Finding, Person, Vendor, Process, or Project). [Pillar 1]
- AC-1.3 Each written file has fully-populated YAML frontmatter using the **real STIG XCCDF / NIST 800-53 Rev 5 / POA&M field names** (verified against DISA sources during /deepen). No invented field names. [Pillar 2: federal-credible]
- AC-1.4 When the agent references a previously-mentioned entity, it writes `[[wikilinks]]` in the body (and quoted in frontmatter list fields). [Pillar 1]
- AC-1.5 Files are written as complete-rewrites (never edit-in-place) to avoid the Obsidian vault cache truncation bug. [Pillar 1]
- AC-1.6 Agent suppresses preamble ("I'd be happy to…") and just calls the tool. Verified via `tool_choice` and tool-description pacing instruction. [Pillar 1]
- AC-1.7 Recovery line "Log that — {name}, {properties}, type {type}" reliably triggers `write_entity` after a missed call. [Pillar 1]

### FR-2 — Agent 2: Scheduled compliance processing

**As** the presenter who served `nlit_cora_agent` the night before,
**I want** `[modules.scheduler]` to fire the CORA pipeline at `0 4 * * * UTC` (= 23:00 EST) without intervention,
**so that** the audience sees a real cron-driven autonomous run with a finished gap report waiting for them.

**Acceptance criteria:**
- AC-2.1 Schedule fires within 60 seconds of cron expression (per scheduler module's `check_interval_seconds = 30` default). [Pillar 1: matches existing module behavior]
- AC-2.2 Pipeline reads `demo-data/system_inventory.md`, `stig_checklist.csv`, `poam_log.csv`, `org_chart.json` — all hand-authored, all using real federal field names. [Pillar 2: federal-credible]
- AC-2.3 `poam_validator` skill surfaces the planted false closure on STIG V-220812 (POA&M closure 2026-03-03 vs system reimaging 2026-03-17). [Pillar 1: deterministic logic, not LLM-dependent]
- AC-2.4 `draft_gap_report` skill writes a markdown report to `workspace/Reports/CORA-Gap-Report-{date}.md` with: false-closure section, top-priority findings, owner-less findings, past-due findings, 14-day remediation calendar. [Pillar 1]
- AC-2.5 Schedule emits `schedule:completed` bus event on success or `schedule:failed` on error; arcui receives both. [Pillar 2: existing event boundary]
- AC-2.6 Pipeline timeout = 600 seconds; circuit breaker disables schedule after 3 consecutive failures (matches `solutions/.../async-scheduler-hardening` recommendations). [Pillar 3: defense in depth]

### FR-3 — Stage UX: arcui + Obsidian

**As** the presenter on stage,
**I want** two windows on the projector: arcui dashboard (left) + Obsidian (right),
**so that** the audience sees both the agent's reasoning/telemetry and the brain populating, side-by-side.

**Acceptance criteria:**
- AC-3.1 arcui dashboard at `localhost:8420` renders live during Agent 1 chat: LLM call timeline, token/cost telemetry, tool invocations. [Pillar 1: existing arcui capability]
- AC-3.2 arcui dashboard adds a new **Schedule History card** rendering last 5 fires + next fire, populated from `schedule:completed`/`schedule:failed` events. [Pillar 2: small extension to existing arcui module boundary]
- AC-3.3 Obsidian renders the `workspace/entities/` graph view live. **Refresh Any View** plugin (or equivalent) installed on demo machine to handle graph auto-refresh on external writes. [Pillar 1: known Obsidian limitation, plugin-mitigated]
- AC-3.4 arcui authenticates via `~/.arcagent/ui-token` (TOCTOU-safe, 0600 perms); auto-connect succeeds on agent startup. [Pillar 3: existing auth path]

### FR-4 — Independence between agents

**As** the architect,
**I want** the two agents to share nothing — no vault, no skills, no DID, no process,
**so that** the demo's claim of "two independent agents" is structurally true and each agent is independently shippable as an example.

**Acceptance criteria:**
- AC-4.1 `team/nlit_soc_agent/` and `team/nlit_cora_agent/` are independent directories with their own `arcagent.toml`, `workspace/`, `tools/`. [Pillar 2]
- AC-4.2 Neither agent references the other's path in any config file. [Pillar 2]
- AC-4.3 Each agent has its own DID generated under `~/.arcagent/keys/` on first run. [Pillar 3]
- AC-4.4 `team/shared/` is not used for any code or skill in this spec (consistent with existing convention: shared is for runtime data, not tools). [Pillar 2]

### FR-5 — No fakes (architectural honesty)

**As** the architect,
**I want** the demo to run on real Arc agent infrastructure with real Claude calls and real scheduler firing,
**so that** the claim "this is what the audience would get if they downloaded Arc tomorrow" is true.

**Acceptance criteria:**
- AC-5.1 No pre-recorded video fallback. No bypass scripts. No demo-only validation that doesn't ship to customers. [Pillar 1: simplicity = no special-case code]
- AC-5.2 Few-shot examples are loaded as documented Anthropic best practice (LangChain benchmark: Sonnet 16%→52% reliability boost) — they would also be in any production deployment. [Pillar 1]
- AC-5.3 Schedule fire is real (`[modules.scheduler]` actually evaluating cron) — not a manual `arcagent run` claimed as overnight. [Pillar 5: no external orchestration]
- AC-5.4 The `arcui` Schedule History card built for the demo becomes a permanent arcui feature (not removed after stage). [Pillar 2: clean module boundary]

## 4. Non-Functional Requirements

### NFR-1 Reliability

- Agent 1 demo runs end-to-end in ≤8 minutes (per original PRD §11). [Pillar 1]
- Agent 2 pipeline completes in ≤300 seconds wall-clock. [Pillar 1]
- 5-rehearsal pre-flight checklist completed before stage (per `/deepen` research). [Pillar 1]

### NFR-2 Modularity

- Zero modifications to `arcagent`, `arcrun`, `arcllm`. [Pillar 2]
- Each agent's skills live entirely in its own `workspace/tools/` (no `[extensions] paths` cross-references). [Pillar 2]
- arcui Schedule History card is one file (`schedule-history.html` partial + ~50 lines JS) — no refactor of existing arcui dashboard. [Pillar 2]

### NFR-3 Security (Pillar 3, personal-tier baseline)

- `write_entity` validates output paths via `resolve_workspace_path()` (path traversal prevention). [Pillar 3]
- All file writes are atomic-rewrite (write to temp + rename), avoiding Obsidian vault cache truncation. [Pillar 3]
- LLM API key sourced from environment variable (`ANTHROPIC_API_KEY`), never written to disk. [Pillar 3]
- Schedule prompts never contain secrets, CUI, or API keys (per scheduler hardening review). [Pillar 3]
- Demo machine uses a **dedicated Anthropic API key** for the demo, separate from any production key (per Meta Connect 2025 lesson learned). [Pillar 3]

### NFR-4 Federal-Ready (deferred but compatible)

- All code paths must be tier-agnostic. No `if tier == "personal"` shortcuts. Tier flips via TOML per ADR-019. [Pillar 3]
- Skill structure follows existing `RegisteredTool` pattern → signing-ready when `arctrust` verification is enabled. [Pillar 3]
- Audit emission already happens via existing arcagent telemetry → `SignedChainSink` swap is a config change. [Pillar 3]

### NFR-5 Performance

- `write_entity` completes in ≤100ms (load template + render + write file). [Pillar 4]
- Obsidian graph view stays interactive at ≤200 entities (well within 500-note smooth threshold per /deepen research). [Pillar 4]
- arcui websocket event flush ≤100ms (matches existing arcui benchmark). [Pillar 4]

## 5. Constraints

- **Hard constraint:** No modifications to `arcagent`, `arcrun`, `arcllm`. The user explicitly stated: "Only adjust the packages or architecture if something is broken." If we discover a genuine break during implementation, surface as a separate spec.
- **Hard constraint:** Local-first. No cloud dependency for the demo. Internet only for the Claude API call.
- **Hard constraint:** Must use real DISA STIG / NIST 800-53 / POA&M field names verified during `/deepen`. Federal audience instantly identifies invented field names.
- **Naming:** Use **CORA** (current name as of 2024-03), not CCRI, in all user-visible code, identity files, and demo dialogue. PRD reference to "CCRI" is historical.
- **Demo machine:** macOS (per existing arc dev environment). Obsidian on macOS handles subdirectory file watching natively.

## 6. Demo Data — Planted Evidence (Agent 2)

| File | Plant |
|---|---|
| `stig_checklist.csv` | V-220812 (RHEL-09-XXXXXX, severity=high, CAT I) marked status=Closed with closure_date=2026-03-03 |
| `poam_log.csv` | POA-2026-003 references V-220812; closure 2026-03-03 |
| `system_inventory.md` | host-alpha (System-A) reimaging_date=2026-03-17 (post-dates closure → false closure) |
| `org_chart.json` | host-beta has no current owner (vacant ISSO field) |

`poam_validator` skill applies deterministic logic (date comparison) — no LLM needed for the surface to fire. AC-2.3 is testable with assertions.

## 7. Anchor Prompts (Agent 1)

5 STIG/security anchor phrasings the presenter uses on stage. Each is rehearsed to reliably trigger `write_entity`. Riffable between (per `/deepen` research):

1. "We just had a meeting about the RHEL upgrade on host-alpha — Jane is the system owner."
2. "SIEM flagged 47 SSH auth failures on host-alpha at 23:14 UTC last night."
3. "Now I'm seeing a DNS anomaly on host-beta at 23:52 UTC. Both hosts are on subnet 10.0.1."
4. "STIG V-220812 was flagged on host-alpha — it's a CAT I, control family CM, CCI-000366."
5. "I'm escalating this to a P1 incident — possible lateral movement between host-alpha and host-beta."

These live in `team/nlit_soc_agent/demo-prompts/stage-anchors.md`. **They are not loaded by the agent** — they're a presenter-side index card.

## 8. Out of Scope (referenced from README)

See `README.md § Out of Scope`. Most notable cuts: `scan_and_connect` engine, `_pending.md` machinery, cross-agent connection, cloud deployment, custom web brain viewer, backup recordings.

## 9. Open Questions for `/implement`

None blocking. Two operational notes for implementation:

- **Q1:** Identity DID generation — first-run automatic via `arcagent.toml [identity] did = ""` (auto-generated under `~/.arcagent/keys/`). Confirm during scaffold that this works without `[vault]` backend (personal tier). Existing `team/my_agent/` already does this — copy pattern.
- **Q2:** arcui Schedule History card — verify before building that `schedule:completed`/`schedule:failed` events actually flow through `[modules.ui_reporter]` to arcui (research showed events are emitted on the bus, but didn't confirm ui_reporter forwards them). If not forwarded, add the event subscription as part of this spec's arcui change.
