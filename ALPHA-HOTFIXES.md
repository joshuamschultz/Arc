# Alpha Release Hotfixes

Living tracker for the alpha hotfix batch. **No issue is done until it is checked
off here with its merge commit.** Every issue in the index must reach `MERGED`
before this batch is complete.

- **Integration branch:** `fix/hotfix-batch` (all fixes land here; merged to `main` only when the batch ships)
- **Started:** 2026-08-29

---

## Process (per issue)

1. **Write it up** — symptom, where it shows, expected vs actual. Recorded below.
2. **Find the cause** — codebase research maps the real root cause (file:line), not the symptom.
3. **Research the fix** — online research finds the current best-practice solution where the fix isn't obvious.
4. **Fix the root cause** — a coding agent implements in an **isolated git worktree**, TDD, honoring `CLAUDE.md`: robust + simple, modular (seam-clean), secure (four pillars), no legacy shims.
5. **Verify** — tests + `ruff` + `mypy --strict` green (frontend: `tsc` + build).
6. **Merge** — merge the worktree branch into `fix/hotfix-batch`, mark `MERGED` + commit. Frontend `static/` is rebuilt once per wave, not per worktree.

**Guardrails:** root cause not band-aid · two-strikes then question the architecture · verify before claiming · no `git add -A` on a shared tree · sub-agents never run destructive git · four pillars (simple, modular, secure, scalable) on every change.

---

## Status legend

| Status | Meaning |
|--------|---------|
| `NEW` | Logged, not yet started |
| `CAUSE` | Root cause being mapped |
| `RESEARCH` | Best-fix research in flight |
| `CODING` | Being implemented in a worktree |
| `VERIFY` | Implemented; tests/gates running |
| `MERGED` | Merged into `fix/hotfix-batch` (commit noted) |
| `BLOCKED` | Needs a decision from Josh (noted inline) |

**Size:** S = small UI/CSS/format · M = backend+frontend fix · L = feature/design work (own mini-spec).

---

## Index

| # | Area | Title | Size | Status | Commit |
|---|------|-------|------|--------|--------|
| H-001 | Home | Approvals + needs bubble up to "NEEDS YOU" | M | NEW | |
| H-002 | Home | Recent Activity shows the sub-process (job) | S | NEW | |
| H-003 | Home | Token Volume in a real unit (M/B), fix axis labels | S | NEW | |
| H-004 | Home | Tasks + Runs counts time-bound to 24h | M | NEW | |
| H-005 | Home | Metrics always live + accurate | M | NEW | |
| H-006 | Home | Home loads with data in < 2s | M | NEW | |
| H-007 | Global | Consistent, legible agent identity (host/type/id/short-name) | M | MERGED | 663d9ea1 |
| H-008 | Fleet | Fleet shows 0 calls though agents ran | M | NEW | |
| H-009 | Agent·Identity | Show all identity does + editable rendered identity.md | M | NEW | |
| H-010 | Agent·Identity | Tool Policy display wrong (shows deny-all; is allow-all) | S | NEW | |
| H-011 | Agent·Inbox | Thread opens below (confusing) → side/expand like email | M | NEW | |
| H-012 | Agent·Runs | Rename Runs→Activity; add identifying metadata | S | NEW | |
| H-013 | Agent·Tools | Two duplicate tool lists → one complete list | M | NEW | |
| H-014 | Agent·Tools | Show tools from builtin/agent/extension/module w/ source id | M | NEW | |
| H-015 | Agent·Prompts | Wrap text in prompt editor/viewer | S | NEW | |
| H-016 | Agent·Knowledge | Interactive graph viewer (hover nodes/edges → metadata) | L | NEW | |
| H-017 | Agent·Knowledge | Use empty screen space (bigger content) | S | NEW | |
| H-018 | Agent·Workspace | Add delete for files (have view/edit, not delete) | M | NEW | |
| H-019 | Messages | Member add: dropdown of agents + operators (multi-operator) | M | NEW | |
| H-020 | Tasks | Board bottom overflows off-screen → responsive/boxed | S | NEW | |
| H-021 | Audit | Not loading (HTTP 500) | M | CODING | |
| H-022 | Audit | Rows unreadable — show what/who/which tool/agent/process | M | NEW | |
| H-023 | Knowledge | View/search chunks (embedded + literal) + metadata | L | NEW | |
| H-024 | Knowledge | Explore connected data (its SQL + its embedded chunks) | L | NEW | |
| H-025 | Knowledge | DB semantic layer: auto-create on connect, view/edit, hit first | L | NEW | |
| H-026 | Knowledge | OKF index.md for doc repos, hosted on ~/arc, refreshed on reindex | L | NEW | |
| H-027 | Knowledge | Promote-to-shared-memory active + filter + shared view | M | NEW | |
| H-028 | Model usage | Stop comparing embedding model to inference (bad savings) | S | NEW | |
| H-029 | Model usage | Apply the LLM-call detail/naming changes here too (H-007) | S | NEW | |
| H-030 | Tools/Skills | Uploading a skill/tool: right place, signed, loaded, injected | M | NEW | |
| H-031 | Tools/Skills | Filters (agent / tool·skill / builtin·agent·ext·module); all show | M | NEW | |
| H-032 | Connections | Cards only need half screen | S | NEW | |
| H-033 | Connections | Connect/save-keys/probe all work in arcui AND arccli | M | NEW | |
| H-034 | Menu | Collapse = a button at top of the menu | S | NEW | |
| H-035 | Menu | Operator mode lives in the operator avatar (top-right) | M | NEW | |
| H-036 | UI | Fully keyboard-drivable | L | NEW | |
| H-037 | UI | Every arcui action has an arccli equal (and vice versa) | L | NEW | |
| H-038 | System | Inject day/time per LLM call (not in cached system prompt) | M | NEW | |
| H-039 | System | Every config in TOML (even off), shown in arcui, in new-agent startup | M | NEW | |
| H-040 | Fleet | arcteam holds multiple agent TYPES (arcagent/hermes/openclaw), shared memory/ui/fleet | L | NEW | |
| H-041 | arcskill | Real skill improvement loop: pick traces → edit ideal → golden set → improve | L | NEW | |
| H-042 | arcskill | Hermes-type skill/tool improvements instilled, working, documented | L | NEW | |
| H-043 | CI | All CI jobs red — **billing** (jobs run 0 steps); code gates green | M | BLOCKED/part-done | e3ab74de |

**Batch exit gate:** every row `MERGED` **and** H-043 green (all CI jobs pass) before the batch ships.

**Advisor:** a standing **Fable** planner/advisor (`Planner`) reviews each issue's plan for correctness, architecture fit (four pillars), and best outcome before it is coded, and reviews the result before merge.

**Planner sequencing (2026-08-29):** Wave0 unblockers (H-043 billing→Josh, H-021) · Wave1 H-007 identity foundation (server-side, DID-join, one component — dependents H-008/H-029/H-022/H-019/H-012/H-002 adopt it) · Wave2 quick-S UI · Wave3 Home/metrics (H-004/5/6/8/1 — one query-layer unit; H-008 is the connector-counter defect family) · Wave4 agent-detail · Wave5 system/plumbing · Wave6 mini-specs (Knowledge cluster H-023→27, H-016 graph, H-041→42 skill, H-040 fleet, then H-036 keyboard, H-037 CLI parity LAST). **Flag:** H-002 (Home activity) / H-012 (agent Runs tab) job-subtitle may be partly covered by the recent run-list work — diff before coding.

---

## Issues

### Home

#### H-001 — Approvals + needs bubble up to "NEEDS YOU"
- **Symptom:** Home "NEEDS YOU" says "all caught up," but approvals/needs exist elsewhere. Everything requiring the operator must surface here.
- **Expected:** Pending approvals, awaiting-review, mapping approvals, held runs, capability requests — all aggregate into NEEDS YOU.
- **Status:** NEW

#### H-002 — Recent Activity shows the sub-process
- **Symptom:** Home "RECENT ACTIVITY" rows show only "Olivia" — no job kind. Same as the runs list fix.
- **Expected:** Reuse the `job` subtitle (Context upkeep / Memory distill / …) under the name.
- **Status:** NEW

#### H-003 — Token Volume in a real unit
- **Symptom:** "TOKEN VOLUME · 24H" chart y-axis all read `0000` (formatter broken); volume should read in K/M/B.
- **Expected:** Axis + tooltip in millions/appropriate unit, human-readable.
- **Status:** NEW

#### H-004 — Tasks + Runs counts time-bound to 24h
- **Symptom:** Home Tasks (231 total) and Runs (193 done…) are all-time; everything else on Home is 24h.
- **Expected:** Tasks + Runs cards show last-24h counts, consistent with the rest of the page.
- **Status:** NEW

#### H-005 — Metrics always live + accurate
- **Symptom:** Home metrics can be stale/inaccurate.
- **Expected:** Live, correct numbers on every load (ties to H-006, H-008).
- **Status:** NEW

#### H-006 — Home loads with data < 2s
- **Symptom:** Home slow to populate.
- **Expected:** First meaningful data < 2s (query bounds, parallel fetch, indexes).
- **Status:** NEW

### Global identity

#### H-007 — Consistent, legible agent identity
- **Symptom:** Sometimes we show the DID, sometimes the file/short name; inconsistent across LLM metrics, model usage, fleet, messages, identity.
- **Expected:** One shared presentation a non-technical user can read: **host** (local), **platform** (arc), **type** (executor), **id** (short DID hash), **short name** (josh_agent / Olivia). DID stays visible, paired with the friendly name. One component reused everywhere.
- **Status:** NEW

### Fleet

#### H-008 — Fleet shows 0 calls though agents ran
- **Symptom:** Fleet view says 0 calls; Olivia demonstrably ran.
- **Expected:** Per-agent call counts are correct (likely a DID/label join or window bug).
- **Status:** NEW

### Agent detail

#### H-009 — Identity: full detail + editable identity.md
- **Symptom:** Identity tab shows config only.
- **Expected:** Everything identity governs, plus a correctly-rendered, editable `identity.md` at the bottom (ADR-029: workspace direct I/O, signed/audited edit path).
- **Status:** NEW

#### H-010 — Tool Policy display wrong
- **Symptom:** Identity → Tool Policy shows `ALLOW ø (deny-all)`, but Tools tab shows `POLICY allow-all`. Contradiction; likely a display/interpretation bug (empty allowlist read as deny-all when the effective policy is allow-all).
- **Expected:** One truthful reading of the effective policy in both places.
- **Status:** NEW

#### H-011 — Inbox thread panel
- **Symptom:** Opening a mail thread pushes it below the list; confusing.
- **Expected:** Side panel (email-client) or inline expand from the row.
- **Status:** NEW

#### H-012 — Runs → Activity + metadata
- **Symptom:** Per-agent "Runs" tab; naming inconsistent with global "Activity"; rows are bare run-ids.
- **Expected:** Rename to Activity; add identifying metadata (job, trigger, turns/tools, time).
- **Status:** NEW

#### H-013 — Merge duplicate tool lists
- **Symptom:** Tools tab shows two near-identical lists (one view/edit, one "capability tools — loader verdicts" with metadata).
- **Expected:** One complete list combining view/edit + metadata (version/source/status/description).
- **Status:** NEW

#### H-014 — Tool source identifiers
- **Symptom:** Can't tell where a tool comes from.
- **Expected:** Show builtin / agent / extension / module with a clear source badge for each tool.
- **Status:** NEW

#### H-015 — Prompt editor wraps text
- **Symptom:** Prompt view/edit overflows horizontally (scrollbar), hard to read/edit.
- **Expected:** Soft-wrap long lines in the prompt viewer + editor.
- **Status:** NEW

#### H-016 — Knowledge graph viewer
- **Symptom:** Only node/link counts; no viewer.
- **Expected:** Interactive graph: render nodes/edges, hover for node metadata, hover edges for relationship, zoom/pan. Self-contained (air-gap; no CDN).
- **Status:** NEW

#### H-017 — Use empty screen space
- **Symptom:** Knowledge/other tabs leave large empty areas.
- **Expected:** Content fills available space responsively.
- **Status:** NEW

#### H-018 — Delete files
- **Symptom:** Workspace file browser: view/edit only, no delete.
- **Expected:** Delete (operator-gated, audited) alongside view/edit.
- **Status:** NEW

### Messages

#### H-019 — Member add dropdown
- **Symptom:** Channel members "agent ref…" is a free-text box.
- **Expected:** Dropdown listing agents + operators (multi-operator ready).
- **Status:** NEW

### Tasks

#### H-020 — Board overflow
- **Symptom:** Kanban bottom overflows off-screen.
- **Expected:** Responsive, contained columns/cards with a clean bottom.
- **Status:** NEW

### Audit

#### H-021 — Audit 500
- **Symptom:** Audit page "Couldn't load data — HTTP 500"; counts 0/0/0.
- **Expected:** Loads reliably (root-cause the 500).
- **Status:** NEW

#### H-022 — Audit row detail
- **Symptom:** Even loaded, rows can't say what was approved/denied, which tool/process/agent.
- **Expected:** Each event shows actor (friendly + DID), action, target tool/process, outcome, decision.
- **Status:** NEW

### Knowledge (global)

#### H-023 — Chunk view/search
- **Symptom:** No way to view/search chunks.
- **Expected:** Search embedded + literal chunks; show metadata (source, scope, hash, classification, score).
- **Status:** NEW

#### H-024 — Explore connected data
- **Symptom:** No explorer for a connection's SQL/DB or its embedded context chunks.
- **Expected:** Browse a connection's tables/schema and its indexed chunks.
- **Status:** NEW

#### H-025 — DB semantic layer
- **Symptom:** No semantic layer for connected databases.
- **Expected:** On connect, auto-generate a semantic layer (DB purpose, tables, columns, example values e.g. `unique.head(10)`); viewable/editable; an agent's DB search **hits the semantic layer first**.
- **Status:** NEW

#### H-026 — OKF doc-repo index.md
- **Symptom:** No per-folder index for document repos (smb/dropbox/s3/onedrive).
- **Expected:** OKF `index.md` per folder/subfolder (what's inside + purpose), hosted under `~/arc` (can't write the remote store), refreshed on every reindex. Reindex is a settable extension→arcagent module.
- **Status:** NEW

#### H-027 — Promote to shared memory
- **Symptom:** Promote-to-shared-memory not visibly active; no filter; no shared view.
- **Expected:** Active promote tool + filter for what shares + a shared-memory view in Knowledge (like agent files, shared).
- **Status:** NEW

### Model usage

#### H-028 — Embedding ≠ inference
- **Symptom:** "Potential savings $494 (100%) by switching to all-MiniLM-L6-v2" compares an embedding model to inference spend. Invalid — can't run inference on MiniLM.
- **Expected:** Compare like-for-like (inference vs inference, embed vs embed); don't suggest an embedder as an inference swap.
- **Status:** NEW

#### H-029 — Model usage detail/naming
- **Symptom:** Same call-detail + agent-naming gaps as LLM metrics.
- **Expected:** Apply H-007 here; per-call detail (what it looked at, embed vs retrieve).
- **Status:** NEW

### Tools / Skills (global)

#### H-030 — Upload path
- **Symptom:** Unsure uploading a skill/tool lands correctly.
- **Expected:** Upload → correct agent tool/skill location → signed → loaded → appears in the context-injected capability list. Verified end-to-end.
- **Status:** NEW

#### H-031 — Filters + full coverage
- **Symptom:** No filtering; unsure all sources show.
- **Expected:** Filter by agent, by tool/skill, by builtin/agent/extension/module; every plugin/module/extension loads and shows.
- **Status:** NEW

### Connections

#### H-032 — Half-screen layout
- **Symptom:** Connection cards use full width unnecessarily.
- **Expected:** Half-width / denser layout.
- **Status:** NEW

#### H-033 — Connect works (arcui + arccli)
- **Symptom:** Need certainty connect/save-keys/probe all work.
- **Expected:** Full connection lifecycle works from both arcui and arccli.
- **Status:** NEW

### Menu

#### H-034 — Collapse button
- **Symptom:** Collapse is a menu row.
- **Expected:** A button at the top of the menu.
- **Status:** NEW

#### H-035 — Operator in avatar
- **Symptom:** Operator mode toggle is a menu row.
- **Expected:** Move to operator avatar (top-right): login, view mode, operator details.
- **Status:** NEW

### UI (global)

#### H-036 — Keyboard-driven
- **Expected:** Full keyboard operation available for those who want it.
- **Status:** NEW

#### H-037 — arcui ↔ arccli parity
- **Expected:** Every arcui action has an arccli equivalent and vice versa (no capability needs the dashboard).
- **Status:** NEW

### System

#### H-038 — Per-call date/time
- **Symptom:** Agent lacks current day/time.
- **Expected:** Inject day/time into each LLM call context (NOT the cached system prompt — it varies per call).
- **Status:** NEW

#### H-039 — Full config surface
- **Expected:** Every config key present in the TOML files even when off; shown in arcui; part of every new-agent startup scaffold.
- **Status:** NEW

### New features (larger)

#### H-040 — Multi-type fleet
- **Expected:** arcteam holds multiple agent TYPES (arcagent + hermes + openclaw + future custom), all using shared arcmemory, arcui, visible in the fleet with as much detail as possible; still message/task/memory-share across types.
- **Status:** NEW

#### H-041 — arcskill improvement loop
- **Symptom:** arcskill doesn't visibly improve skills.
- **Expected:** After N uses, select traces (call+result), edit outcomes toward ideal, build a golden set, run improvements against it.
- **Status:** NEW

#### H-042 — Hermes skill/tool improvements
- **Expected:** The Hermes-type skill/tool improvement research (done months ago) fully instilled, working, and documented.
- **Status:** NEW

### CI

#### H-043 — All CI jobs red
- **Symptom:** Every CI job fails in 2–4 seconds.
- **Root cause (found):** Every recent run executed **0 steps across all jobs** (Lint job: started 18:57:11, completed 18:57:13, `steps: []`). Jobs die at runner **setup**, before any step. This is an **account/infrastructure** failure — the standard signature of GitHub Actions **billing/spending limit reached** or Actions disabled for the repo/org — not a code failure. Logs are pruned (BlobNotFound), consistent with never-started jobs.
- **Split:**
  - **H-043a (infra) — BLOCKED on Josh:** check GitHub → Settings → Billing → Actions spending limit, and repo Settings → Actions enabled. Code cannot fix this.
  - **H-043b (code) — mine:** repo had real lint/format drift (3 ruff errors + 24 files needing `ruff format`) that would fail Lint once the runner works; plus `mypy packages/*/src --strict` must be clean repo-wide. Driving these to green so the matrix passes the moment H-043a is resolved.
- **Status:** BLOCKED (H-043a on Josh) · H-043b in progress
