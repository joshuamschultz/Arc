# SPEC-020 — PLAN: NLIT 2026 Demo (Local Arc Agent Build)

## Status

| | |
|---|---|
| **Status** | COMPLETE |
| **Phase** | 6 of 6 |
| **Total tasks** | 38 (+ 4 framework fixes) |
| **Completed** | 38 |
| **Remaining** | 0 |

Status progression: PENDING → **COMPLETE (all phases done) ✅** → VERIFIED (after live rehearsal in `REHEARSAL.md`).

## Bonus framework fixes shipped during this build

| Fix | Where | What |
|---|---|---|
| FIX-1 | `arccli/commands/agent.py` | `arc agent create` auto-registers in arcteam (workspace_path correctly points at the `workspace/` subdir) |
| FIX-3 | `arcllm/trace_store.py` | `JSONLTraceStore.__init__` warns when the workspace path is misregistered (defense in depth) |
| FIX-4 | `arcagent/core/tool_registry.py` | Tool policy filters at registration: deny/allow lists *skip* tools (with warning + audit), don't crash agent startup |
| FIX-5 | `arcagent/modules/ui_reporter`, `arcui/types.py` | `ui_reporter` subscribes to `schedule:completed` + `schedule:failed`; new `scheduler` layer routes these to arcui's Schedule History card |

## Phasing

Six phases, dependency-ordered. Each phase ends with a verification gate. Phases 1–4 are TDD per task; Phases 5–6 are integration + rehearsal.

| Phase | Title | Tasks | Gate |
|-------|-------|-------|------|
| 1 | Scaffolding & vendor checks | 6 | Both agent dirs exist, both `arcagent serve` runs cleanly with no tools, arcui connects |
| 2 | Agent 1 — entity templates | 9 | All 9 templates valid YAML + Obsidian-renderable |
| 3 | Agent 1 — `write_entity` skill + few-shots | 6 | Unit tests pass; integration test fires write_entity from real Claude on 5/6 anchors |
| 4 | Agent 2 — skills + demo data | 10 | Unit tests pass; planted V-220812 false closure surfaces deterministically |
| 5 | arcui Schedule History card + scheduler wiring | 4 | Card renders last-fire + next-fire from real `schedule:completed` events |
| 6 | End-to-end + rehearsal | 3 | 5-rehearsal pre-flight checklist complete |

## Phase 1 — Scaffolding & vendor checks

**Goal:** Two empty agent projects come up cleanly and connect to arcui. No demo logic yet.

- [ ] **T1.1** Create `team/nlit_soc_agent/` with `arcagent.toml` (per SDD §3.1), empty `workspace/`, empty `tools/__init__.py`. Run `cd team/nlit_soc_agent && arcagent serve` — verify clean startup, DID auto-generated, ui_reporter connects to arcui. [Pillar 1]
- [ ] **T1.2** Create `team/nlit_cora_agent/` with `arcagent.toml` (per SDD §4.1), empty `workspace/`, empty `tools/`, empty `demo-data/`. Verify `arcagent serve` clean startup, DID auto-generated, scheduler module enabled. [Pillar 1]
- [ ] **T1.3** Verify `[modules.scheduler]` is functional: from a chat with `nlit_cora_agent`, register a test schedule firing every 60 seconds with prompt "Say hello". Confirm fires, `schedule:completed` event flows to arcui telemetry. Delete test schedule. [Pillar 1]
- [x] **T1.4** Verify `~/.arcagent/ui-token` exists (or generate via `arc ui start` per SPEC-019). Confirm both agents auto-connect to arcui. **Three-step registration is required for arcui visibility (each agent):** (1) `arc team register {name} --type agent --roles executor --workspace /path/to/team/{name}`, (2) **`arc team backfill-workspaces --apply`** — this fixes the workspace_path to point at the `workspace/` subdirectory (without it, JSONLTraceStore appends `/traces` to the agent root and finds nothing), (3) restart `arc ui start`. Confirm "Trace stores: N" in arcui startup output increases by 1 per new agent. **Without backfill, traces hit disk correctly but arcui shows nothing — symptom would be confused for "trace forwarding broken".** [Pillar 2 — arcteam owns identity/discovery; arcui only renders what arcteam knows]
- [ ] **T1.5** Confirm `ANTHROPIC_API_KEY` is a **dedicated demo key** (not user's primary). Document key location in `team/nlit_soc_agent/REHEARSAL.md`. [Pillar 3]
- [ ] **T1.6** Install Obsidian on demo machine. Open `team/nlit_soc_agent/workspace/` as vault. Install **Refresh Any View** plugin from community plugins. Verify auto-refresh works on a manually-created `.md` file. [Pillar 1]

**Gate:** Both agents serve cleanly. arcui shows both agents connected. Obsidian opens the SOC vault. Test schedule fires. ✅ → Phase 2.

## Phase 2 — Agent 1 entity templates

**Goal:** All 9 entity templates exist and produce Obsidian-valid markdown when rendered with sample properties.

- [ ] **T2.1** Write template `workspace/templates/System.md` with real federal field names from /deepen §5: `system_name`, `system_id`, `fips_199_categorization`, `system_owner`, `isso`, `issm`, `ao`, `ato_date`, `ato_expiration_date`, `fisma_boundary`, `primary_os`, `ip_range`, `criticality`, `data_classification`. **TEST:** unit test renders template with sample properties → asserts valid YAML, asserts Obsidian-quote-safe (colons, dates, wikilinks). [Pillar 1, Pillar 2]
- [ ] **T2.2** Write template `workspace/templates/Event.md`. Fields: `event_id`, `event_type` (auth_failure|dns_anomaly|priv_escalation|...), `timestamp_utc`, `source_system`, `affected_hosts` (wikilink list), `severity_xccdf`, `count`. TEST: same shape as T2.1.
- [ ] **T2.3** Write template `workspace/templates/STIG-Reference.md`. Fields per /deepen §5: `vuln_id`, `rule_id`, `stig_id`, `severity`, `severity_cat`, `weight`, `cci` (list), `control_family` (NIST 800-53 Rev 5 codes), `title`, `vuln_discussion`, `check_content`, `fix_text`, `status`. TEST: same.
- [ ] **T2.4** Write template `workspace/templates/Incident.md`. Fields: `incident_id`, `priority` (P1|P2|P3|P4), `created_utc`, `affected_systems` (wikilink list), `correlation_narrative`, `confidence` (high|medium|low), `status` (Open|Triaged|Mitigated|Closed), `assigned_to` (wikilink). TEST: same.
- [ ] **T2.5** Write template `workspace/templates/Finding.md`. Distinguishes Finding from Vulnerability per /deepen §5 Q8. Fields: `finding_id`, `stig_reference` (wikilink to STIG-Reference), `system` (wikilink), `status` (Open|Not a Finding|Not Applicable|Not Reviewed), `severity_cat`, `owner` (wikilink to Person), `due_date`, `poam_reference`, `notes`. TEST: same.
- [ ] **T2.6** Write template `workspace/templates/Person.md`. Fields: `name`, `role` (ISSO|ISSM|AO|System Owner|Sysadmin|...), `email`, `team`, `owns_systems` (wikilink list), `assigned_findings` (wikilink list). TEST: same.
- [ ] **T2.7** Write template `workspace/templates/Vendor.md`. Fields: `name`, `category` (OS|EDR|SIEM|HBSS|ACAS|...), `contact`, `provides_systems` (wikilink list), `support_contract`, `contract_expiration`. TEST: same.
- [ ] **T2.8** Write template `workspace/templates/Process.md`. Fields: `name`, `category` (SOP|runbook|playbook), `owner` (wikilink), `applies_to` (wikilink list), `last_reviewed`, `next_review_due`. TEST: same.
- [ ] **T2.9** Write template `workspace/templates/Project.md`. Fields: `name`, `status` (Planning|Active|Blocked|Complete), `owner` (wikilink), `affected_systems` (wikilink list), `start_date`, `target_date`, `description`. TEST: same.

**Gate:** All 9 templates exist. Each renders valid YAML when filled with sample properties. Obsidian opens a sample of each without warnings. ✅ → Phase 3.

## Phase 3 — Agent 1 `write_entity` skill + few-shots

**Goal:** `write_entity` works end-to-end from a real Claude call.

- [ ] **T3.1** Implement `workspace/tools/write_entity.py` per SDD §3.4. Factory pattern from `packages/arcagent/src/arcagent/tools/write.py`. **TDD:** write 9 unit tests first (one per entity type), each asserting (a) file written to correct path, (b) valid YAML, (c) wikilinks rendered, (d) atomic write (temp + rename). [Pillar 1, Pillar 3]
- [ ] **T3.2** Add path-traversal test: `write_entity` with `name="../../../etc/passwd"` rejected via `resolve_workspace_path`. [Pillar 3]
- [ ] **T3.3** Add error-path tests: missing template → returns `"Error: ..."` string (not raises); unknown entity_type → returns error.
- [ ] **T3.4** Write `workspace/identity.md` per SDD §3.2 — sections, tool-trigger language, decision framework, lessons learned including CCRI→CORA rename and Finding/Vulnerability/Weakness distinction. [Pillar 1]
- [ ] **T3.5** Write `workspace/few-shots.json` with 3 message-format examples per SDD §3.3. Implement bootstrap loader (extension or context-init) that prepends them to first conversation. [Pillar 1]
- [ ] **T3.6** **Integration test (slow, runs against real Anthropic):** start agent, send each of the 5 PRD §7 anchor prompts, assert ≥4 result in `write_entity` tool calls (target ≥5/6 in rehearsal but 4/5 here is the CI gate). Document any anchor that consistently fails — adjust either the anchor or the tool description.

**Gate:** All 9 unit tests + path-traversal + error-path tests green. Integration test passes ≥4/5 anchors firing. Identity + few-shots loaded. ✅ → Phase 4.

## Phase 4 — Agent 2 skills + demo data

**Goal:** CORA pipeline runs end-to-end, planted false closure surfaces deterministically.

- [ ] **T4.1** Implement `workspace/tools/read_document.py`. CSV → list[dict] (DictReader); MD with frontmatter blocks → list[dict]; JSON → dict. **TDD:** test each format with valid + malformed inputs. [Pillar 1]
- [ ] **T4.2** Hand-author `demo-data/system_inventory.md` per SDD §4.5. 4 systems with real federal frontmatter fields. **host-alpha** has `reimaging_date: 2026-03-17`. [Pillar 2: federal-credible]
- [ ] **T4.3** Hand-author `demo-data/stig_checklist.csv` per SDD §4.5. 40 rows total. **V-220812 row** must have `system_id=host-alpha, status=Closed, closure_date=2026-03-03, severity=high, severity_cat="CAT I"`. Plus V-220745 row with no owner, V-220718 known false-positive flagged.
- [ ] **T4.4** Hand-author `demo-data/poam_log.csv` per SDD §4.5. 25 rows. **POA-2026-003 row** references V-220812 with closure_date 2026-03-03. Real POA&M field names from /deepen §5 Q5.
- [ ] **T4.5** Hand-author `demo-data/org_chart.json` per SDD §4.5. 8 staff records. host-beta's ISSO field is null/blank.
- [ ] **T4.6** Implement `workspace/tools/stig_cross_reference.py` — pure logic. Joins `stig_checklist` against `system_inventory` by `system_id`. Returns list of findings sorted by `severity_cat` desc. **TDD:** test with the planted data → assert 40 findings produced, sorted correctly.
- [ ] **T4.7** Implement `workspace/tools/poam_validator.py` — pure logic, **deterministic**. For each POA&M entry with `status=Closed`: look up the referenced `vuln_id` in `stig_checklist`, find the `system_id`, look up that system's `reimaging_date` in `system_inventory`. If `closure_date < reimaging_date` → false closure. **TDD assertion:** with the planted data, exactly ONE false closure returned (POA-2026-003 / V-220812 / host-alpha). Twenty-four other POA&M rows pass. [Pillar 1: testable, deterministic]
- [ ] **T4.8** Implement `workspace/tools/draft_gap_report.py`. LLM call with prompt: "Format this gap report. Sections in order: False Closures (start with this if any), High-Severity Findings, Findings With No Owner, Past-Due Findings, 14-Day Remediation Calendar." Inputs: validator output + cross-reference output + org chart. **TDD:** snapshot test of structure (regex assertions for required section headers).
- [ ] **T4.9** Write `workspace/identity.md` for CORA agent per SDD §4.3. Includes lessons-learned section with V-220718 false-positive note + CCRI→CORA rename note + severity nomenclature note.
- [ ] **T4.10** **Integration test (slow):** run the full CORA prompt against `nlit_cora_agent`. Assert: report file written to `Reports/CORA-Gap-Report-{date}.md`; file contains "V-220812"; file contains "false closure" (case-insensitive); file mentions host-alpha; file's "False Closures" section is non-empty.

**Gate:** All unit tests green. `poam_validator` test asserts exactly one false closure on planted data. Integration test passes. ✅ → Phase 5.

## Phase 5 — arcui Schedule History card + scheduler wiring

**Goal:** When the cron fires, arcui shows it.

- [ ] **T5.1** Verify `[modules.ui_reporter]` forwards `schedule:completed` and `schedule:failed` bus events to arcui. Investigate `packages/arcagent/src/arcagent/modules/ui_reporter/__init__.py` event subscriptions. If NOT forwarded, add subscription (~10 lines). [Pillar 2: extends existing module boundary]
- [ ] **T5.2** Add Schedule History card UI per SDD §5.3. Files: `packages/arcui/src/arcui/static/components/schedule-history.html`, `packages/arcui/src/arcui/static/js/schedule-history.js`. Card subscribes to events, maintains bounded list of last 5 fires, computes next-fire from cron expression (use a small JS croniter or pull from event payload if available). [Pillar 1, Pillar 2]
- [ ] **T5.3** Register the CORA cron schedule in `nlit_cora_agent`: chat with the agent once, "Create a schedule, type=cron, expression='0 4 * * *', prompt='Run the CORA compliance audit. Read system_inventory.md, stig_checklist.csv, poam_log.csv, org_chart.json from ./demo-data/. Cross-reference STIGs against systems. Validate POA&M closures. Surface false closures and unowned findings. Draft gap report to ./workspace/Reports/CORA-Gap-Report-{date}.md.'". Verify persisted in `~/.arcagent/schedules.json`.
- [ ] **T5.4** **Integration test:** trigger the schedule manually (via `schedule_run` tool or temp short interval). Confirm: schedule fires, pipeline runs, report appears, `schedule:completed` event flows to arcui, Schedule History card updates with new entry showing duration + status.

**Gate:** Schedule fires on real cron. Card updates within 1 second of fire completion. Report file written. ✅ → Phase 6.

## Phase 6 — End-to-end + rehearsal

**Goal:** Demo runs, 5-rehearsal pre-flight passes.

- [ ] **T6.1** Write `team/nlit_soc_agent/REHEARSAL.md` and `team/nlit_cora_agent/REHEARSAL.md`. Include: 5-rehearsal pre-flight checklist (Baseline T-7d → Variation T-5d → Recovery T-3d → Environment T-1d → Dress AM-of), anchor prompts index card content, recovery line ("Log that — {name}, {properties}, type {type}"), environment checklist (API key set, network confirmed, arcui running, Obsidian open with vault, Refresh Any View enabled, schedule registered, time-zone verified). [Pillar 1]
- [ ] **T6.2** Run rehearsals 1–4 (T-7d through T-1d). Document each in REHEARSAL.md: how many anchors fired, what failed, what was changed, time-on-stage. After rehearsal 1, expect to refine identity.md tool-trigger language and possibly the few-shots. [Pillar 1]
- [ ] **T6.3** Run rehearsal 5 (Dress, AM-of). Full uninterrupted run, no corrections. If a tool call fails, observe and document; do NOT restart. This rehearsal sets the floor expectation for stage. ✅ → READY FOR STAGE.

## Verification Checklist (run before marking COMPLETE)

- [ ] Both agents serve cleanly with `arcagent serve`
- [ ] Both agents auto-connect to arcui
- [ ] Obsidian renders the SOC vault graph view; Refresh Any View installed
- [ ] All 9 templates valid YAML, all unit tests green
- [ ] `write_entity` integration test fires ≥4/5 anchors against real Claude
- [ ] `poam_validator` deterministically surfaces exactly V-220812 from planted data
- [ ] CORA pipeline integration test produces report mentioning V-220812 + false closure
- [ ] arcui Schedule History card updates on real schedule fire
- [ ] Rehearsals 1–5 documented in REHEARSAL.md
- [ ] No modifications to `arcagent`, `arcrun`, `arcllm` (`git diff packages/{arcagent,arcrun,arcllm}/` is empty)
- [ ] arcui changes scoped to one card + maybe ~10 lines of ui_reporter event subscription

## Rollback

This spec adds files only — no destructive operations. Rollback = delete `team/nlit_soc_agent/`, `team/nlit_cora_agent/`, and revert the small arcui addition. Demo schedule auto-disables if circuit breaker trips after 3 failures.

## Open Items Tracked Against /implement

Per PRD §9:
- **Q1** Identity DID auto-generation under personal tier (no [vault]) — verify in T1.1 and T1.2.
- **Q2** ui_reporter forwarding of `schedule:*` events — verify in T5.1; if missing, fix is in scope (~10 lines).

If Q2 reveals missing arcagent module work that exceeds ~10 lines, surface as a separate sub-spec rather than expanding this one.
