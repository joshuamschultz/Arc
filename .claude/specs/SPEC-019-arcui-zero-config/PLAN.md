# PLAN: ArcUI Zero-Config Launch

**Spec**: SPEC-019 | **Status**: PENDING | **Date**: 2026-04-26
**Branch**: `feature/SPEC-019-arcui-zero-config`

Single PR. Phases below are review checkpoints, not separate merges. Each task is independently testable; phases are sequenced for dependency reasons. TDD throughout per `~/.claude/rules/standard/tdd-enforcement.md`.

## Counts

- Total tasks: **22**
- Completed: **0**
- Remaining: **22**

## Phase 1 — Registry foundation (workspace_path)

Goal: arcteam knows where each agent lives. Backfill working for the 5 existing entities.

- [ ] **T1.1** Add `workspace_path: str | None = None` to `arcteam.types.Entity` with a load/serialize round-trip test (load existing JSON record without field; assert `workspace_path is None`). [Pillar 2]
- [ ] **T1.2** Add `EntityRegistry.update(entity)` method — write-through with audit emission `entity.updated`. Unit test for write + read-back. [Pillar 2]
- [ ] **T1.3** Add `--workspace` flag to `arc team register`. Default = `Path.cwd()`. Resolve to absolute. Validate: must exist, must be a directory, must not contain `~` or env vars in stored form. Test all four cases. [Pillar 1, Pillar 3-SR-6]
- [ ] **T1.4** Add `arc team backfill-workspaces` subcommand. `--dry-run` default; `--apply` to write. Idempotent. `--team-dir <path>` (defaults to `team`). Tests: dry-run no-op, apply writes, second apply no-op, missing TOML skipped, malformed TOML skipped with warning. [Pillar 1]
- [ ] **T1.5** Run backfill against the 5 currently-registered agents in `~/.arc/team/`. Verify each entity gets the absolute path to its workspace. (Manual step, log result in spec README.)

**Phase 1 done when**: `arc team entities --json` shows `workspace_path` for all 5 agents, all unit tests pass, `mypy --strict` clean.

## Phase 2 — Trace store iteration

Goal: arcllm exposes async iteration over historical records; arcui can warm-start from N stores.

- [ ] **T2.1** Add `iter_records() -> AsyncIterator[dict[str, Any]]` to `TraceStore` Protocol in `packages/arcllm/src/arcllm/trace_store.py`. [Pillar 2]
- [ ] **T2.2** Implement `JSONLTraceStore.iter_records()` — reads `traces-*.jsonl` files in chronological filename order; yields one parsed record per line. Uses async file I/O if `aiofiles` available, else `asyncio.to_thread` over sync file read. Memory bounded to per-line. Tests: empty store, single file, multi-file ordering, malformed line tolerance (skip + warn). [Pillar 4]
- [ ] **T2.3** Add `RollingAggregator.warm_start_multi(stores)` to `packages/arcui/src/arcui/aggregator.py`. Heap-merges by `timestamp` field across stores. Calls existing `ingest()` per record. Tests: empty list, single store equivalence to `warm_start`, three-store interleaved correctness, deterministic order on tie. [Pillar 1, Pillar 4]
- [ ] **T2.4** Performance test: 5 stores × 1000 records each, assert `warm_start_multi` completes in <500ms and peak memory <100MB. [NFR-1, NFR-2]
- [ ] **T2.5** Implement `FederatedTraceStore` in `packages/arcui/src/arcui/federated_store.py`. Methods: `query`, `get`, `iter_records`, `close`. Compound cursor format: base64-encoded JSON list of per-store sub-cursors. Tests: empty stores list, single store passthrough equivalence, three-store query with `?agent=` filter, pagination across stores (cursor round-trip), `get()` first-hit-wins, `close()` propagates. [Pillar 2, Pillar 1]
- [ ] **T2.6** Wire `FederatedTraceStore` selection in `arccli/commands/ui.py`: zero stores → `None`; ≥1 stores → `FederatedTraceStore(stores)`. Test both branches. [Pillar 2]
- [ ] **T2.7** Integration test: `/api/traces?agent=my_agent` against a federated store with 3 workspaces, only one of which contains my_agent records — assert returned records all carry `agent_label == "my_agent"` and only from the matching store. [FR-4]

**Phase 2 done when**: warm_start_multi + FederatedTraceStore tested and benchmarked.

## Phase 3 — Loopback browser bootstrap

Goal: `arc ui start` opens the browser already authenticated when bound to loopback.

- [ ] **T3.1** Add `_resolve_trace_stores(args)` to `packages/arccli/src/arccli/commands/ui.py`: query the registry. Skip entities with `workspace_path is None` or non-existent path with warning to stdout. [Pillar 2]
- [ ] **T3.2** Add `_maybe_open_browser(host, port, viewer_token)` to ui.py. Loopback bind (`127.0.0.1`, `localhost`, `::1`) only. URL = `http://{host}:{port}/#auth={viewer_token}`. Mock `webbrowser.open` in tests; assert called with correct URL on loopback, NOT called on `0.0.0.0`. [Pillar 1, SR-4]
- [ ] **T3.3** Wire `_maybe_open_browser` to fire after uvicorn ready event. Use uvicorn lifespan hook. Test: subprocess launch + WS port-listen check + assertion that browser open was attempted. [Pillar 2]
- [ ] **T3.4** Add `/api/health` unauthenticated route (returns `{"status": "ok"}`) for the autoconnect probe. ~5 LOC. [SDD Open Q3]
- [ ] **T3.5** JS bootstrap in `packages/arcui/src/arcui/static/assets/arc-shell.js`: parse `location.hash` for `auth=`, store `arcui_viewer_token` in localStorage, fire `history.replaceState(null, '', pathname + search)` BEFORE any other code runs. Place as first script in `<head>`. Test: jsdom or playwright headless — load `index.html#auth=ABC`, assert localStorage set + URL hash empty. [SR-2]
- [ ] **T3.6** Update REST/WS clients in `arc-shell.js` to send `Authorization: Bearer <token>` from `localStorage.getItem('arcui_viewer_token')` on every request. Confirm fallback to manual-paste flow when localStorage empty. [SR-5]

**Phase 3 done when**: `arc ui start` on a clean machine with the 5 backfilled agents opens browser, shows dashboard with traces, no token paste.

## Phase 4 — Agent autoconnect probe

Goal: `arc agent run|chat|serve` automatically streams to UI when token file + URL both present.

- [ ] **T4.1** Add `_should_auto_enable(token_file, url) -> tuple[bool, str]` helper to `packages/arcagent/src/arcagent/modules/ui_reporter/__init__.py`. Tests: file absent, file wrong owner (mock `os.getuid` and `stat.st_uid`), file loose perms (0644), URL down (httpx connect error), URL 404, URL 200 → enable. Each case returns expected (bool, reason). [Pillar 3-SR-1]
- [ ] **T4.2** Modify `UIReporterModule.startup()`: `enabled = false` returns early; otherwise call `_should_auto_enable` and connect on True. On True, audit `ui.agent_autoconnect`. Test both branches. [Pillar 1, FR-9, FR-10]
- [ ] **T4.3** Add `httpx` to `arcagent` deps if not present. (Already used elsewhere; verify.) [Pillar 4]
- [ ] **T4.4** Probe latency benchmark: 100 sequential probes against unreachable URL must complete in <5s (50ms timeout × 100). Document in test docstring. [NFR-5]

**Phase 4 done when**: launching `arc agent chat team/my_agent` with UI running streams events without `--ui`, without TOML edit; without UI silently runs normally.

## Phase 5 — Audit & session tracking

Goal: every UI session start, every agent autoconnect, every browser bootstrap is logged. NIST AU-2 satisfied uniformly.

- [ ] **T5.1** Add `ui.session_start` event type to `packages/arcui/src/arcui/audit.py`. Fields: `session_id`, `uid`, `remote_addr`, `auth_method`. [SR-3]
- [ ] **T5.2** Add `ui.agent_autoconnect` event type. Fields: `agent_id`, `uid`, `url`, `reason`. Emitted from T4.2. [SR-3]
- [ ] **T5.3** Add session tracking to `packages/arcui/src/arcui/auth.py`: hash(viewer_token) → session_id mapping; on first authenticated request per session, emit `session_start` with derived `auth_method`:
  - Request from loopback + token matches one issued via URL → `browser_bootstrap`
  - Request from non-loopback OR loopback with no URL-issued mapping → `manual_token`
  Test all three labeling paths. [SR-3]

**Phase 5 done when**: audit log after a clean session contains exactly one `ui.session_start` per browser, one `ui.agent_autoconnect` per agent that connected, and zero events from the no-UI path.

## Phase 6 — Lint, type, coverage

- [ ] **T6.1** `ruff check .` clean across all five touched packages.
- [ ] **T6.2** `mypy --strict` clean; no new `# type: ignore` without explicit comment justifying.
- [ ] **T6.3** Branch coverage ≥ 75% on new code; line coverage ≥ 80%.
- [ ] **T6.4** Manual verification: full acceptance criteria walkthrough on the local machine with the 5 registered agents. Log results in spec README under "Learnings".

**Done when**: all 22 tasks checked, all phases done, branch ready for review.

## Out of scope (explicitly)

- Multi-host registry (would require central state store; SPEC-019 is single-machine).
- Workspace path redaction across remote viewers (defer; SPEC-019 assumes registry is local file, not cross-host).
- `arc ui start` graceful handling of registry corruption — fails with explicit error message; full self-healing deferred.
- Migration to mTLS by default on loopback (loopback intentionally relies on UID + token model per Pillar 3 lockdown-via-config).

## Pillar Mapping

Every task above ties to at least one principled-coder pillar. Quick index:

| Pillar | Tasks |
|--------|-------|
| Simplicity | T1.3, T1.4, T2.3, T2.5, T3.2, T3.5, T4.2 |
| Modularity | T1.1, T1.2, T2.1, T2.5, T2.6, T3.1, T3.3, T4.2 |
| Security | T1.3 (SR-6), T3.5 (SR-2), T3.6 (SR-5), T4.1 (SR-1), T5.1–T5.3 (SR-3, AU-2) |
| Scalability | T2.2, T2.4, T4.4 |
