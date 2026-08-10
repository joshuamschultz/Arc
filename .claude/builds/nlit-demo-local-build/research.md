# nlit-demo-local-build — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** none — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Build Decisions: nlit-demo-local-build

**Date:** 2026-04-27
**Brainstorm:** [.claude/brainstorms/2026-04-27-nlit-demo-local-build.md](brainstorms/2026-04-27-nlit-demo-local-build.md)
**State:** [.claude/builds/nlit-demo-local-build/state.json](builds/nlit-demo-local-build/state.json)
**PRD:** [.claude/NLIT2026-Demo-PRD.md](NLIT2026-Demo-PRD.md)
**Tier target:** personal (federal-ready as product seed)
**Top principle:** Showcase real Arc capability, no fakes. Local first.

### Design at a glance

Two **independent** Arc agents in `team/`, each demonstrating one mode of "agent as coworker."

| | Agent 1 — `nlit_soc_agent` | Agent 2 — `nlit_ccri_agent` |
|---|---|---|
| Mode | Conversational, live on stage | Autonomous, scheduled overnight |
| Trigger | I chat (STIG/security topics) | `[modules.scheduler]` cron 23:00 |
| Workspace | `entities/{type}/{name}.md` from templates | gap report + run history |
| Skill set | `write_entity` (template-driven) | `read_document`, `stig_cross_reference`, `poam_validator`, `draft_gap_report` |
| Stage UX | arcui dashboard + Obsidian split-screen | arcui scheduler history + the Markdown report |

Shared between them: **nothing**. No vault, no skills, no process, no DID. Two clean Arc agents.

### Decisions

| # | Decision | Answer | Principle |
|---|----------|--------|-----------|
| 1 | Project location | Two independent `team/{nlit_soc_agent,nlit_ccri_agent}/` projects. No shared anything. | Modularity |
| 2 | Execution pattern | Both served (long-lived). Agent 1 chat-driven; Agent 2 + scheduler module fires overnight. | Simplicity (matches existing arc serve pattern) |
| 3 | Skill packaging | Each agent's own `tools/`. No `[extensions] paths` sharing. | Simplicity (no shared code, no shared bugs) |
| 4 | Agent 1 vault structure | `workspace/entities/{type}/` + **template-driven `write_entity`**. 9 entity templates: System, Event, STIG-Reference, Incident, Finding, Person, Vendor, Process, Project. Each has rich frontmatter the agent fills in. Agent writes `[[wikilinks]]` inline; Obsidian renders graph natively. NO `scan_and_connect` engine. | Simplicity + demo value (templates make structure visible — that's the wow) |
| 5 | Agent 2 cron | Built-in `[modules.scheduler]` (croniter, circuit breaker, timeout, telemetry). Stays inside Arc — schedule history is part of the demo. | Real Arc capability (vs external unix cron) |
| 6 | Stage visibility UX | **arcui** (web, `localhost:8420`, via existing `[modules.ui_reporter]`) + **Obsidian** split-screen on projector. | Real Arc capability (ui_reporter is already wired) |
| 7 | Determinism guards | Low temp (0.2) + strong `identity.md` per PRD + rehearsal. NO eval pass, NO Pydantic strict-mode demo-only validation, NO pre-recorded fallback. | No fakes (demo behavior must match production) |
| 8 | Test data | Hand-author Agent 2's 4 PRD files (system_inventory.md, stig_checklist.csv with V-220812 planted, poam_log.csv with date mismatch, org_chart.json). Agent 1: 5-7 STIG/security anchor prompts I'll riff between. | Simplicity + control (exact planted findings) |

### Explicitly out of scope (deferred)

- Pass 4 (proximity scan) — not needed at all (no `scan_and_connect` engine)
- Backup screen recordings + offline/Nemotron fallback
- Cloud deployment (Azure)
- Custom hosted brain viewer (Obsidian IS the viewer)
- Cross-agent connection ("money moment" from PRD) — replaced by two distinct demonstrations of agent capability
- `_pending.md`, `_context.md`, `connected_to[]` PRD vault metadata — unused without the inference engine

### What changed from the PRD

The PRD assumed two analyst agents writing into a shared Atlas vault, with `scan_and_connect` discovering cross-agent links automatically. Build session reframed as **two independent agents demonstrating two different modes** (conversational entity capture + autonomous scheduled processing). This:

- Cut ~10 of the 14 PRD skills (no `scan_and_connect`, no `write_relationship`, no `_pending.md` machinery, no `memory_recall` separate from arc's bio_memory)
- Eliminated cross-agent coordination complexity
- Made each agent independently shippable as an example
- Preserved the 9-entity-type richness via templates instead of via 4-pass inference

### Next step

Ready for `/specify nlit-demo-local-build` — decisions are tightly scoped and unambiguous.

---

### Research Insights — nlit-demo-local-build (2026-04-27 /deepen pass)

#### 1. Scheduler — confirmed solid, with two gotchas

- `[modules.scheduler]` is real and usable. Schedule entries created via the `schedule_create` tool with cron expression (croniter syntax). Engine fires `agent_run_fn(entry.prompt, tool_choice={"type": "any"})` — **schedules fire a natural-language prompt, not a tool call directly.** The agent receives the prompt and decides what tools to use.
- **Cron is UTC by default** (`scheduler.py:159`). For 11pm EST → use `0 4 * * *` (4am UTC). Verify timezone before stage.
- **No queue persistence across restart**: in-flight entries are lost on agent crash; metadata persists.
- Hardening already applied per `solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md`: Unicode normalization, update field allowlist, circuit-breaker re-reads from store.
- Bus events emitted: `schedule:completed`, `schedule:failed` — these flow to arcui and audit.
- Recommended config for CCRI overnight: `timeout_seconds=600`, `circuit_breaker_threshold=3`.
- Files: `packages/arcagent/src/arcagent/modules/scheduler/{__init__.py:20-126, scheduler.py:357-401, models.py:119-189, config.py:13-28, store.py:71-88, tools.py:44-82}`

#### 2. arcui — telemetry ready, scheduler view NOT built

- arcui dashboard (`packages/arcui/src/arcui/static/index.html`) currently shows: LLM calls, tokens, latency, cost — **all live-updating** with sub-100ms event flush.
- Event taxonomy works: `agent:init/ready/pre_tool/post_tool/error`, `llm:call_complete`, scheduler bus events.
- WebSocket auth via `~/.arcagent/ui-token` (TOCTOU-safe), auto-connect on agent startup, 1000-event buffered local deque, decorrelated jitter reconnect.
- 300 unit tests, recent commits (clean refactor 3 weeks ago) — production-quality plumbing.
- ⚠️ **Gap:** arcui has NO frontend rendering for: scheduler fire history, next-run prediction, tool-call timeline, agent state machine. The events flow over the wire but no card displays them.
- **Implication for Act 2:** "Audience sees cron history in arcui" needs either (a) ~2-3 hours of frontend work to add a schedule card OR (b) show schedule history via the filesystem (`schedules.json`) or audit log instead, OR (c) demo Act 2 visibility entirely through Obsidian (the report file appears + audit events fire). Option (c) is most honest and zero new code.

#### 3. Existing skill patterns — copy-don't-invent

- **Template to copy:** `packages/arcagent/src/arcagent/tools/write.py` — uses factory pattern `create_tool(workspace, *, allowed_paths=None) -> RegisteredTool`.
- Tool fields: `name, description, input_schema (hand-written JSON Schema dict, not Pydantic), transport=ToolTransport.NATIVE, execute, source, classification ("state_modifying"|"read_only"), capability_tags`.
- Auto-discovered from `workspace/tools/*.py` or `workspace/extensions/*.py` — no toml registration needed.
- identity.md is **plain markdown, not YAML** — sections like Core Truths / Values / Personality / Decision Framework / Lessons / Boundaries / Available Tools / Behavior. Used as agent context, not auto-parsed.
- **No Jinja2 in arc** — use `string.Template` (stdlib) or f-strings for templates. Keep deps minimal.
- `entities/` is already a workspace convention.
- Workspace must use `resolve_workspace_path()` for path traversal protection.
- Tool execute() returns `str` (not raises) — prefix errors with `"Error: "`.

#### 4. Obsidian behavior — two real gotchas, both fixable

- **YAML wikilinks need quoting**: `system: "[[host-alpha]]"`. Bare `[[...]]` in lists can be parsed as YAML sequences and break the graph. Wikilinks in frontmatter DO create graph edges (since Obsidian 1.4).
- **Property types Obsidian recognizes**: text, list, number, checkbox, date, datetime. No native "link" type — wikilinks-as-strings just work.
- **Performance**: smooth at <500 notes, fine at 500-1000. Stage demo (50-200 notes) is well within sweet spot.
- ⚠️ **Graph view does NOT auto-refresh on external file writes.** Notes appear in file explorer but graph requires manual click-refresh or the [Refresh Any View](https://github.com/mnaoumov/obsidian-refresh-any-view) plugin. **Action item:** install Refresh Any View plugin OR plan to click-refresh between stage prompts.
- ⚠️ **Vault cache truncation bug**: when external process writes a file LARGER than previous version, Obsidian's cache transiently overwrites disk with stale content for 1-2s. Mitigation: always write NEW files (never edit-in-place) during the demo.
- Recommended plugins: **Extended Graph** (47k+ downloads, node colors/shapes by metadata, scales by attributes, SVG export — strongest stage option), Folders-to-Graph, 3D Graph.
- Graph color-by-tag works natively; configure groups for each `type:` value (System=blue, Event=red, STIG=yellow, etc.).
- YAML pitfalls to avoid: unquoted colons (`title: STIG V-220812: SSH...` breaks), tab indentation, numeric-looking IDs (`id: 2024-001` parses as date — quote `"FIND-2024-001"`).
- File-write pattern that works: write COMPLETE files with full frontmatter every time, never edit in place.

#### 5. STIG/CCRI schema — real field names verified

- **CCRI was renamed to CORA (Cyber Operational Readiness Assessment) on 2024-03-01** by JFHQ-DODIN. Federal NLIT 2026 audience knows it as CORA. **Action item:** update naming throughout (PRD, identity files, demo dialogue) to CORA, or add a "formerly CCRI" parenthetical.
- **STIG XCCDF field names verified**: `vuln_id` (V-XXXXXX), `rule_id` (SV-XXXXXXrXXXXXXX_rule), `stig_id` (e.g., RHEL-09-211010), `severity` (high|medium|low — NOT CAT I/II/III in YAML), `weight`, `cci` (list of CCI-XXXXXX), `vuln_discussion`, `check_content`, `fix_text`. Real example documented (V-257777 RHEL 9).
- **CCI format**: `CCI-XXXXXX` (six zero-padded digits). Many-to-many with NIST 800-53.
- **NIST 800-53 Rev 5 control families** (20 total, two-letter codes): AC, AT, AU, CA, CM, CP, IA, IR, MA, MP, PE, PL, PM, PS, **PT** (new), RA, SA, SC, SI, **SR** (new). Use these exact codes in `control_family` field.
- **Severity dual-naming**: XCCDF XML uses `high|medium|low`. STIG Viewer UI shows CAT I/II/III. Frontmatter should carry both: `severity_xccdf: high` + `severity_cat: "CAT I"` for clarity.
- **POA&M required fields**: poam_id, weakness_description, weakness_detector_source (STIG|ACAS|pen test|self-assessment), severity (Critical|High|Moderate|Low), scheduled_completion_date, milestones (≥2 required), point_of_contact, status (Draft|Ongoing|Delayed|Pending Verification|Completed|Risk Accepted|Waiver), corrective_action_plan, vendor_dependency, vendor_dependent_product_name.
- **Remediation SLAs (FedRAMP/CMS)**: Critical 15-30d, High 30d, Moderate 90d, Low 180-365d.
- **System inventory standard fields**: system_name, system_id, fips_199_categorization (Low|Moderate|High), system_owner, isso, issm, ao, ato_date, ato_expiration_date, fisma_boundary, primary_os, ip_range, criticality (Mission Critical|Essential|Supporting), data_classification (Unclassified|CUI|Secret|TS).
- **Critical distinction federal audience knows**: Finding ≠ Vulnerability ≠ Weakness. STIG scan output = "findings"; open findings become POA&M "weaknesses"; ACAS/CVE-based scan results = "vulnerabilities". **Don't conflate** — the Finding template should reference STIG-Reference, not CVE.

#### 6. Live LLM demo determinism — three changes recommended

- **Temperature=0** (not 0.2) is the documented best practice for tool-calling reliability (Anthropic docs). Note: Opus 4.7 ignores temperature entirely (adaptive sampling); Sonnet 4.5/4.6 honor temperature=0.
- **Few-shot examples in MESSAGE format are the single highest-leverage technique** — LangChain benchmark: Sonnet 3 went from 16%→52% with just 3 message-format examples; Haiku 11%→75%. This is NOT a "demo-only hack" because real customers using these agents will get the same boost from message-format few-shots in their own setup. **Worth reconsidering Decision 7 to include 3 message-format few-shot examples in agent setup.**
- **Tool description is the most powerful steering lever** (Anthropic: "by far the most important factor in tool performance"). For `write_entity`, the description should explicitly state: "Call this tool whenever the user mentions a specific IT/security entity by name. Do not explain or ask for confirmation — just call this tool immediately." This explicit pacing instruction prevents preamble.
- **`tool_choice={"type": "tool", "name": "write_entity"}`** forces a tool call on turns where I know I'll mention an entity — no hedging is architecturally possible. Worth using for known entity-mention turns.
- **Opus 4.7 uses tools LESS often than 4.6** by default. Anthropic explicitly recommends adding tool-trigger instructions to system prompt for 4.7. If using 4.7, identity.md must be more explicit about when to call tools.
- **Recovery patterns**: imperative reformulation ("Log that — host-alpha, subnet 10.0.1.x, type system") reliably triggers tools after a missed call. "write_entity for that" with explicit tool name almost always works.
- **Demo failure modes to avoid (Bard, Gemini, Meta Connect)**: factual recall without grounding (we have grounding — agent extracts from MY input), faked demos (we're honest), shared API keys with rate-limit collisions (use a dedicated demo key).
- **Pre-flight checklist (verbatim)**: 5 rehearsals (Baseline T-7d, Variation T-5d, Recovery T-3d, Environment T-1d, Dress AM-of). Anchor card at podium with 6 entity-mention phrasings. Dedicated API key, secondary hotspot ready.

---

### Action Items Surfaced by Research (need user decision)

These are reconsiderations triggered by deepen findings, NOT new questions:

1. **Update Decision 7 (determinism)**: temperature 0 (not 0.2), and add 3 message-format few-shot examples to Agent 1's setup. Few-shots aren't a "fake" — they're a documented Anthropic best practice that real customers would use. Materially raises tool-call reliability.
2. **Update Decision 6 (stage UX)**: arcui doesn't have a scheduler-history view. Three paths: (a) build a small UI card (~2-3 hrs), (b) show schedule history from filesystem/audit log via terminal, (c) skip the "arcui scheduler view" claim entirely and demo Act 2 through Obsidian alone (the report file appears, audit events fire in arcui telemetry).
3. **CCRI vs CORA naming**: rename throughout to CORA (current correct name as of 2024-03), or keep CCRI with parenthetical. Federal audience will know.
4. **Obsidian Refresh Any View plugin**: install on demo machine (graph view doesn't auto-update on external writes). Or plan manual click-refresh between stage prompts.
5. **Update Decision 4 (entity templates)**: use real STIG XCCDF field names — `vuln_id`, `rule_id`, `stig_id`, `cci`, `severity`+`severity_cat`, `vuln_discussion`, `check_content`, `fix_text`. NIST 800-53 Rev 5 control family codes (the real 20). POA&M with proper status taxonomy. Distinguish Finding/Vulnerability/Weakness.


### Resolutions from /deepen (2026-04-27)

| # | Original decision | Updated to |
|---|------------------|------------|
| Naming | `nlit_ccri_agent` | **`nlit_cora_agent`** — full rename to CORA throughout (agent dir, identity, demo dialogue, anchor prompts). PRD's "CCRI" stays as historical reference but live demo uses CORA. |
| 4 (templates) | Generic field names | **Use real STIG XCCDF + NIST 800-53 Rev 5 + POA&M field names from research.** STIG-Reference template uses `vuln_id`, `rule_id`, `stig_id`, `severity` (high\|medium\|low), `severity_cat` (CAT I\|II\|III), `cci` (list), `vuln_discussion`, `check_content`, `fix_text`. Finding template uses POA&M-style fields with proper status taxonomy. System template uses real federal inventory fields (fips_199_categorization, isso, issm, ao, ato_date, fisma_boundary, primary_os, ip_range, criticality, data_classification). Distinguishes Finding vs Vulnerability vs Weakness explicitly. |
| 6 (stage UX) | arcui + Obsidian split-screen | **Same plus build a small arcui scheduler card (~2-3 hrs frontend work)** to render schedule history (last 5 fires + next fire). Custom event type + simple HTML card. Becomes part of arcui going forward — not demo-only. |
| 7 (determinism) | temp 0.2, no few-shots | **temp=0 + 3 message-format few-shot examples loaded as pre-conversation turns.** Documented Anthropic best practice (Sonnet: 16%→52% tool-calling reliability with 3 few-shots; LangChain benchmark). Real customer pattern, not a demo hack. Also: explicit tool-trigger instructions in identity.md + tool description, `tool_choice={"type": "tool", "name": "write_entity"}` on known entity-mention turns. |
| Obsidian | Vanilla setup | **Install Refresh Any View plugin** on demo machine (graph view doesn't auto-refresh on external writes). Verify in rehearsal. |

**Items 1, 2, 3, 5, 8 unchanged.** Project location, execution pattern, skill packaging, scheduler mechanism, test data — all stand as originally decided.


---

---
