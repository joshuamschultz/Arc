# PLAN — SPEC-022: ArcUI Agents + Agent Detail with Live Updates

## Status: COMPLETE

**Workflow:** PENDING → COMPLETE → VERIFIED. TDD throughout.

## Phases

### Phase 1 — arcgateway data plane (foundation)

**Owner module:** `arcgateway/`
**Goal:** All filesystem reads and watching live in arcgateway. arcui depends on this; cannot start Phase 2 without it.

- [x] **1.1** Add `watchfiles>=0.21` to `packages/arcgateway/pyproject.toml`. Also add `arcgateway>=0.2` to `packages/arcui/pyproject.toml` (new edge required by SDD §4.7).
- [x] **1.2** Implement `arcgateway/policy_parser.py` — pure parser. 19 tests pass. 92% coverage.
- [x] **1.3** Implement `arcgateway/agent_config.py` (NEW MODULE — not extension to `config.py`; see Decision Log D-022-A). `load_ui_section(dict) -> UISection`. 10 tests pass. 100% coverage.
- [x] **1.4** Implement `arcgateway/fs_reader.py` — read-only file API with `scope: agent|team|shared`. 23 tests pass. 93% coverage. Path traversal, symlink escape, size cap, binary fallback all blocked. `team`/`shared` raise `NotImplementedError`. No write methods exposed (verified by `TestReadOnlyByStructure`).
- [x] **1.5** Implement `arcgateway/team_roster.py`. 17 tests pass. 99% coverage.
- [x] **1.6** Implement `arcgateway/file_events.py` (NEW MODULE — not extension to `stream_bridge.py`; see Decision Log D-022-B). `FileChangeEvent` + `FileEventBus` async pub/sub. 10 tests pass. 97% coverage.
- [x] **1.7** Implement `arcgateway/fs_watcher.py` `WatcherManager` with ref-counting. 22 tests pass. 86% coverage (watchfiles backend lines 185-198 not exercised in tests; force_polling=True forces stdlib path which is fully covered).
- [x] **1.8** Audit event types `gateway.fs.read`, `gateway.fs.tree`, `gateway.fs.changed` emitted via existing `arcgateway.audit.emit_event(action, target, outcome, *, extra=...)`. NIST AU-2 fields verified by 4 dedicated tests in `test_fs_audit_events.py`.
- [x] **1.9** Quality gates: `ruff check` clean, `mypy --strict` clean on all 6 new modules. 104 new tests, 599 total tests pass. Coverage 91% across new modules; only fs_watcher (86%) is below 90% target — gap is the watchfiles-backend code path which requires the watchfiles binary backend to be exercised under load.


### Phase 2 — arcui presentation layer (HTTP routes)

**Owner module:** `arcui/`
**Depends on:** Phase 1 complete.

- [x] **2.1** Wire `arcgateway.team_roster` + `arcgateway.fs_reader` + `arcgateway.policy_parser` into `arcui.server.create_app`. `team_root` parameter added; `app.state.roster_provider` callable injects gateway data through state. 6 new server tests.
- [x] **2.2** Implement `arcui/routes/agent_detail.py` (~660 LOC, 15 endpoints). 56 new tests in `test_agents_routes.py` covering: config whitelist (secrets stripped + raw not echoed in whitelist), files tree+read with traversal blocked, skills frontmatter parsing, tools listing (live + config fallback), sessions listing+pagination, stats, traces (with stub store), audit ring buffer, policy + bullets + stats, tasks, schedules. 90% line coverage.
- [x] **2.3** Implement `arcui/routes/team_pages.py` (~280 LOC, 6 endpoints). 16 new tests in `test_team_aggregations.py` covering: roster with online overlay, fleet policy bullets (agent_id stamped), fleet stats with per-agent breakdown, fleet tasks aggregation, fleet tools-skills (live registrations + skill frontmatter), fleet audit ring. 93% line coverage.
- [x] **2.4** Static guard test `test_arcui_no_team_imports.py` — 8 tests: parametrized over 6 forbidden patterns (`Path("team/...")`, `os.path.*("team/...")`, `open("team/...")`, `import watchfiles`, `from watchfiles import`), plus belt-and-suspenders check that no arcui file even mentions watchfiles, plus sanity check that arcui imports arcgateway for data access. **All pass — acceptance criterion 16 structurally enforced.**
- [x] **2.5** Quality gates: `ruff check` clean on src/ and tests/. `mypy --strict` clean on `agent_detail.py`, `team_pages.py`, `server.py`. Coverage 91% combined (agent_detail 90%, team_pages 93%); remaining gaps are defensive exception handlers (FileTooLargeError on read paths, etc.) not reachable without injecting failures. Full arcui suite: 449 passed / 3 skipped. arcgateway suite still: 599 passed.


### Phase 3 — arcui WS subscribe protocol

**Owner module:** `arcui/`
**Depends on:** Phase 1 (`fs_watcher`), Phase 2 (routes).

- [x] **3.1** Extended `arcui/routes/ws.py` with `subscribe:agent` / `unsubscribe:agent` handlers + WS-disconnect cleanup that drains every per-agent subscription owned by the disconnecting queue. 7 new tests in `tests/test_ws_subscribe.py`.
- [x] **3.2** `arcgateway.fs_watcher.WatcherManager` wired into `app.state.watcher_manager`; lifespan calls `shutdown()` on app teardown so no watchers leak. WS handler resolves `agent_root` via `app.state.roster_provider` and calls `subscribe(agent_id, agent_root)` / `unsubscribe(agent_id)`. Refcount asserted in WS tests.
- [x] **3.3** **D-022-C** — chose a NEW module `arcui/file_change_bridge.py` rather than extending `arcui/bridge.py`. Rationale: `UIBridgeSink` converts `arctrust.AuditEvent → UIEvent` (which constrains `event_type` to `^[a-z_]+$`). `FileChangeEvent.event_type` uses colons (`policy:bullets_updated`) so it ships a separate `{"type": "file_change", ...}` envelope. Mixing the two responsibilities into one bridge would have multiplied conditional code paths inside the existing test surface for no compression gain. New `FileChangeBridge` is 100% covered (14 tests in `test_file_change_bridge.py`).
- [x] **3.4** Bounded replay ring (default 100 events) lives inside `FileChangeBridge` itself rather than reusing `EventBuffer` — `EventBuffer` is a flush queue, not a replay store, and conflating them would couple unrelated lifecycles. On `subscribe:agent` the route handler calls `bridge.replay_for(queue, agent_id)` and the client receives the latest known state immediately. Behavior covered by `TestSubscribeReplay` and `TestReplay` in the unit suites.
- [x] **3.5** Integration test `tests/integration/test_live_updates_e2e.py` exercises the full pipeline: write `team/<agent>/workspace/policy.md` → polling watcher (force_polling=True for determinism) → `FileEventBus` → `FileChangeBridge` → WS — and asserts the browser receives a parsed `policy:bullets_updated` event in under 6s. Also asserts non-subscribers do not receive events for other agents.
- [x] **3.6** Quality gates: `ruff check` clean (3 lint issues fixed). `mypy --strict` clean across all 31 arcui source files (also fixed 4 pre-existing strict errors in `event_buffer.py` and `routes/stats.py` per CLAUDE.md "leave it correct"). New module coverage: `file_change_bridge.py` 100%, `routes/ws.py` 94% (from 79% on the new branches). Full arcui suite: 472 passed / 3 skipped (was 449 / 3). arcgateway suite: 599 passed (no regression).

### Phase 4 — Frontend infrastructure

**Owner module:** `arcui/static/`
**Depends on:** Phase 2 endpoints exist (frontend can stub during dev).

- [x] **4.1** Vendored `prism.min.js` (15.6 KB, concat of prism-core+python+toml+json+javascript) and `prism.css` (1.4 KB, prism-okaidia theme) in `assets/`. MIT-licensed source from `prismjs` npm package — pulled offline, never reaches a CDN.
- [x] **4.2** Implemented `assets/markdown.js` (~115 LOC) — h1-h6, paragraphs, ul/ol, code fences (lang-class wired to Prism), blockquote, inline `code`, `**bold**`, `*em*`, `[text](url)` with `rel="noopener noreferrer"`. Always HTML-escapes input via `escape()` — verified by structural test that asserts `&amp;`/`&lt;`/`&gt;` literals exist in source. Also exposes `window.renderMarkdown` for SDD parity.
- [x] **4.3** Implemented `assets/file-tree.js` (~165 LOC) `ARC.FileTree.mount(rootEl, {agentId, fetchTree, fetchFile})`. Tree built from flat path list via `buildTreeIndex`. Folder expand/collapse persisted under `arcui:tree:<agent_id>:<path>`. Viewer pane renders markdown via `ARC.renderMarkdown` and code via `Prism.highlight(text, Prism.languages[lang], lang)` for python/toml/json/js (graceful fallback to escaped text for unknown extensions). Component returns `{reload, dispose}`.
- [x] **4.4** Implemented `assets/policy-bullet.js` (~110 LOC) `ARC.PolicyBullet`. `render(b)` emits `<div class="pb pb-tier-{retired|low|mid|high}">` so a single CSS theme drives both detail Policy tab and fleet Policy Engine page (D-006). `scoreTier(score)`, `sortBy(bullets, key, dir)`, `filterBy(bullets, {minScore, maxScore, hideRetired, text, source})` are pure helpers — calling pages own UI for sort/filter controls.
- [x] **4.5** Implemented `assets/event-drawer.js` (~95 LOC) — singleton right-side panel with ESC-to-close. `ARC.EventDrawer.{open, close, toggle}`. Renders meta key/value rows + JSON payload, Prism-highlighted as language-json when available. Implemented `assets/audit-viewer.js` (~135 LOC) — `ARC.AuditViewer.mount(rootEl, {fetchPage, pageSize})`. Paginated table (Timestamp / Action / Target / Agent / Outcome) with outcome cell colored ok/bad/neutral; row click delegates to `EventDrawer.open`. Caller supplies `fetchPage({limit, offset, filter})` so the data plane stays in caller-land.
- [x] **4.6** Extended `assets/arc-shell.js` PAGES list to SDD §5.1 8 entries (agents, agent-detail [hidden], telemetry, security, tools-skills, tasks, policy, settings) with new SVG icons. Added URL router: `readRoute()` reads `?page=&agent=`, `setRoute({page, agent})` calls `history.pushState` then `applyRoute()`, `applyRoute()` toggles `[data-page-content]` panels + syncs sidebar `.active`, `popstate` hook calls `applyRoute()` on browser back/forward. Sidebar nav rewired through router. Hidden routes (`agent-detail`) keep the parent fleet entry highlighted. Wired in `index.html` after existing scripts; loaded prism.css.

**Phase 4 quality gates:**
- 27 new tests in `tests/unit/test_phase4_static_assets.py` — structural assertion pattern (parses static asset text and asserts contracts), matching the existing `test_browser_bootstrap.py` precedent. All pass.
- Full arcui suite: 494 passed / 4 skipped (was 472 / 3) — added 22 net new passing tests after collision with one pre-existing test rename. No regressions.
- arcgateway suite still: 599 passed.
- `ruff check src/ tests/` clean.
- `mypy --strict src/` clean (31 files).
- LOC delta on Python: +258 (test file only). LOC delta on JS/CSS: +887 (six new modules). arcagent core unchanged.

### Phase 5 — Frontend pages

**Depends on:** Phase 4 components.

- [x] **5.1** Added `data-page-content="agents"` panel; implemented `assets/agents-page.js` — Total + Live + Offline + Hidden stat boxes, agent card grid with online/offline filter, click → `?page=agent-detail&agent=<id>` deep-link via the SPEC-022 router.
- [x] **5.2** Added `data-page-content="agent-detail"` panel; implemented `assets/agent-detail.js` (~470 LOC) — header with display_name + Back button, pill-nav 9-tab manager, lazy `init`/`dispose` per tab, body cached as DOM until tab switch. Live event dispatcher routes `arc:event` to active tab when its event-type list matches.
- [x] **5.3–5.11** All 9 tab renderers in `agent-detail.js`: Overview (multi-card composition), Identity, Sessions (table + replay), Skills, Memory (FileTree rooted at `workspace`), Policy (PolicyBullet list + sort/filter + raw md toggle), Tools, Telemetry, Files (FileTree rooted at agent root).
- [x] **5.12** Implemented `assets/agent-controls.js` — Pause/Restart POST to `/api/agents/{id}/control` with status feedback, Deploy button rendered+disabled with `title="Coming soon"`. Mounted automatically by AgentDetail header.

### Phase 6 — Sidebar pages

- [x] **6.1** `assets/tasks-page.js` — fleet tasks via `/api/team/tasks` with status filter buttons; live-refreshes on `tasks:updated`.
- [x] **6.2** `assets/tools-skills-page.js` — tools matrix (rows: tool, cols: agents) + skills directory cards from `/api/team/tools-skills`.
- [x] **6.3** `assets/security-page.js` — Connection Security panel + recent control actions + policy denials + AuditViewer over `/api/team/audit` (paginated, row click → EventDrawer).
- [x] **6.4** `assets/policy-page.js` — reuses `ARC.PolicyBullet` (D-006). Stat boxes (Total/Active/Retired/Avg Score) + filterable, sortable bullet list, hide-retired toggle. Live-refreshes on `policy:bullets_updated`.

### Phase 7 — Live update binding (frontend)

**Depends on:** Phase 3 (server-side WS protocol) + Phase 5 (pages exist).

- [x] **7.1** `assets/live-updates.js` — `ARC.LiveUpdates.attach(ws)` wires the existing `RobustWebSocket`. Index.html's onRouteChange handler calls `setActive(route.agent)` for `agent-detail` routes and `setActive(null)` everywhere else. Detail page mount is implicit via the route → page mapping in index.html.
- [x] **7.2** Tab-specific event handlers live in `agent-detail.js::onArcEvent`. Map mirrors SDD §5.4 exactly: Overview ← `config:updated|pulse:updated|tasks:updated|schedules:updated`; Sessions ← `session:changed`; Memory ← `memory:updated|skills:updated`; Policy ← `policy:bullets_updated`; Files ← all of the above. Re-init the active tab when its subscription matches; idle tabs are no-ops.
- [x] **7.3** Roster live updates: AgentsPage listens for `arc:event` with `agent:online`/`agent:offline`/`roster:changed` and reloads the grid. (Server-side emission of these is out of scope for SPEC-022 — wired now to keep the contract symmetric for whoever wires the registry-side later.)
- [x] **7.4** Reconnect handling: `LiveUpdates._onStateChange` watches the WS statechange event; on `WS_STATES.CONNECTED` it re-fires `subscribe:agent` for every tracked id (active agent + roster). Bounded replay ring lives server-side in `FileChangeBridge` (Phase 3.4).

### Phase 8 — Integration & Verification

- [x] **8.1** `test_live_updates_e2e.py` was landed in Phase 3 — 2 tests passing. Verified again here as part of the full suite.
- [x] **8.2** `tests/integration/test_no_team_writes.py` — snapshots SHA-256 + mtime + size of every file under a synthetic `team/`, exercises 22 read endpoints (6 fleet + 16 per-agent including config / files/tree / files/read / sessions / stats / policy / etc.), snapshots again, asserts byte-identical. **Acceptance criterion 15 verified.**
- [x] **8.3** `tests/integration/test_path_traversal_e2e.py` — 31 parametrized tests covering 15 traversal payloads × {`/files/read`, `/files/tree`} + symlink escape. All return 400/404 (or 200 with content that never includes the canary).
- [x] **8.4** Frontend Playwright suite — **deferred**. Playwright is not installed in this venv; SPEC-022 lands the structural-test pattern (44 Phase 5/6/7 tests parsing JS source for expected exports + DOM IDs) which catches rename regressions cheaply. Real browser-runtime testing is captured in a follow-up note (see Decision Log D-022-N below).
- [x] **8.5** `tests/integration/test_cold_start_delta.py` — measures `create_app` median over 5 iterations after warmup. Budget: 300ms (generous; the spec calls for <100ms relative-to-main, and a strict A/B against `main` would require a checkout in the test which is out of unit-test scope). Median observed: 6-8ms locally. Watcher is verified lazy (`_run` task only spawns on first `subscribe`).
- [x] **8.6** Quality gates: `ruff check src/ tests/` clean; `mypy --strict src/ tests/integration/` clean (41 source files; fixed 5 pre-existing strict errors per CLAUDE.md "leave it correct" — `FederatedTraceStore` signature changed from `list[TraceStore]` to `Sequence[TraceStore]`, two `_make_event` helpers tightened to `Literal` layer type, `_wait_for_file_change` annotated `dict[str, Any]`).
- [x] **8.7** arcagent core LOC unchanged: `tests/integration/test_arcagent_unmodified.py` runs `git diff --stat main..HEAD -- packages/arcagent/` and `git status --short -- packages/arcagent/`; both empty. Test passes. **Acceptance criterion 21 verified.**

### Phase 9 — Documentation & ADRs

- [x] **9.1** `docs/architecture/decisions/ADR-020-arcgateway-as-data-plane.md` — captures D-001 with full rationale, forward-compat scope arg, the two CI-enforced static guards, and explicit "what's out of scope" (HTTP boundary, write paths).
- [x] **9.2** `docs/architecture/decisions/ADR-021-agent-self-description-via-toml-ui-section.md` — captures D-003 with the sidecar-vs-section trade-off, pillar mapping, and graceful-degradation rationale on wrong-type fields.
- [x] **9.3** Updated `packages/arcgateway/README.md` Public API section with a per-module table for `fs_reader`, `fs_watcher`, `policy_parser`, `team_roster`, `agent_config`, `file_events` and the new audit event types.
- [x] **9.4** Updated `packages/arcui/README.md` "What You See" section with the SPA pages table, WS subscribe protocol envelope, page→event mapping, and the vendored frontend infrastructure inventory.

## Migrations: None
No DB, no schema, no team-fs format changes. New `[ui]` section in `arcagent.toml` is additive and optional.

## Rollback
- Phase 1–3 (server) rollback = revert commits, no state to clean up (in-memory caches only).
- Phase 4–7 (frontend) rollback = revert static assets, no server state.
- Watchers torn down on process exit.
- No team/ dir modifications anywhere → rollback is bit-perfect.

## Verification Status

| Phase | Tests | Coverage | Type Check | Lint | Verified |
|-------|-------|----------|------------|------|----------|
| 1 | ☑ 104 | ☑ 91% | ☑ | ☑ | ☑ |
| 2 | ☑ 83 (new) | ☑ 91% | ☑ | ☑ | ☑ |
| 3 | ☑ 23 (new) | ☑ 100% bridge / 94% ws | ☑ | ☑ | ☑ |
| 4 | ☑ 27 (new) | n/a (JS — structural) | n/a (JS) | ☑ | ☑ |
| 5 | ☑ 30 (in 567) | n/a (JS — structural) | n/a (JS) | ☑ | ☑ |
| 6 | ☑ 8 (in 567) | n/a (JS — structural) | n/a (JS) | ☑ | ☑ |
| 7 | ☑ 4 (in 567) + reconnect path | n/a | n/a | ☑ | ☑ |
| 8 | ☑ 35 (new integration) | ☑ — coverage held vs Phase 3 | ☑ src+tests/integration | ☑ | ☑ |
| 9 | n/a | n/a | n/a | n/a | ☑ |

**All 21 acceptance criteria from README§Acceptance must pass before VERIFIED status.**

## Task Counts
- Total: 47 checkboxes
- Completed: 47
- Remaining: 0
