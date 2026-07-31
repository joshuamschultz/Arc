# SPEC-056 — Mission Control · PLAN

## Deepening Summary

**Deepened on:** 2026-07-12
**Sections enhanced:** 7
**Solutions referenced:** 1
**Skills matched:** arc scheduler/messaging/pairing modules (codebase-grounded), solutions: async-scheduler-hardening-6agent-review

### Key Findings
- The atomic single-owner claim already exists — copy arcgateway/pairing.py:788-794 verbatim: `UPDATE tasks SET owner=? WHERE id=? AND owner IS NULL` returning `rowcount==1`. No app locks, no event-sourcing.
- arcstore's MUTABLE directory plane (SPEC-032) is designed but NOT yet built — arcstore today is insert-once telemetry only. Phase A must land on the SPEC-032 mutable plane or build the first mutable `tasks` table itself.
- Owner identity is the ONE real deviation from the scheduler template: scheduler's _State carries no identity; mirror the MESSAGING module's _runtime (it receives identity/agent_did via configure() and stores st.identity). Owner-only mutation is net-new logic.
- Audit is emitted CENTRALLY by the tool registry keyed on each tool's `classification` (read_only vs state_modifying) — tools do NOT call emit themselves. Corrects SDD §7.
- arcteam: `mentions=[assignee_did]` set directly is clobbered by apply_mentions (mentions.py:44) — put `@assignee_handle` in the body instead; add a `TASK_ASSIGNED="task_assigned"` enum member; a DM to `agent://assignee` makes Phase C's 'only assignee wakes' hold even before SPEC-055.
- arcui: re-point the read endpoints via an `Observe.tasks()` reader (opaque TasksResponse = zero-touch schema/web); copy files_write.py's gate→guard→write→audit for the operator mutation + a 409 on in_progress; zero-stale via the existing `tasks:updated` FileEventBus, never re-add polling.

### New Risks Discovered
- SPEC-032 mutable plane unbuilt → Phase A scope may expand to build arc's first mutable arcstore table (estimate/sequencing risk).
- A non-atomic read-check-then-write pattern exists at arcteam/registry.py:88-100 — must NOT be copied for ownership; the capacity/continue-current check must run INSIDE the claim transaction.
- Classification no-write-down can REFUSE an assignment envelope to an under-cleared assignee (messenger.py:243-267) — keep the body minimal (id + terse title) or the assign fails at federal tier.
- The module-bus priority architecture guard (test_module_bus_priority_assignments.py:53-72) likely needs a `tasks` priority entry, or make architecture-tests fails.
- SPEC-055's `_should_activate` gate is still spec-only (grep: no matches) — the channel-scoped 'only the assignee wakes' path depends on it; only DM-addressing works today.


TDD throughout (test → red → implement → green). Quality gates per phase: ruff,
mypy --strict, package suite; tsc for arcui. The two deepen-surfaced dependency risks
— **SPEC-032 mutable arcstore plane** and **SPEC-055 activation gate** — are ABSORBED
as **Phase 0** and built first; no external spec dependencies remain.

Status: `[ ]` todo · `[~]` in progress · `[x]` done.

## Phase 0 — Absorbed prerequisites (build first; 0A ∥ 0B, different packages)

### Phase 0A — arcstore mutable directory plane (SPEC-032 slice) [arcstore]
- [ ] 0A1 — `mutable_records(collection,key,value,updated_at, PK(collection,key))` table + DDL/migration in the arcstore sqlite backend (WAL/busy_timeout/BEGIN IMMEDIATE). Tests: schema/roundtrip.
- [ ] 0A2 — `read/write/delete/query` seam on the mutable plane (mirror the backend Protocol). Tests: roundtrip + filters.
- [ ] 0A3 — **atomic conditional write** `update_if(collection,key,set,where) -> bool` (rowcount>0), pattern from pairing.py:788-794. Tests: 100-run `asyncio.Event` stress — exactly one winner ([[feedback_concurrency_tests_must_interleave]]).
- [ ] 0A4 — audit on every mutable write via `arctrust.audit.emit` (fail-open). Tests: audit emitted with actor_did.

### Phase 0B — mention-scoped activation (SPEC-055) [arcagent] — closes SPEC-055
- [ ] 0B1 — `_should_activate(msg, identity)` predicate + unit tests (critical / mentions-empty=DM+broadcast / mentioned / not-mentioned). *(TDD)*
- [ ] 0B2 — gate `_handle_incoming` on it; ack-and-ignore non-activating (no deliver_fn/agent_run_fn, no follow_up). Tests: a channel msg mentioning others wakes no run + is acked (no RetryableDeliveryError).
- [ ] 0B3 — ruff + mypy --strict + arcagent messaging suite; deploy to olivia; live-verify one @mention → 1 `loop.checkpoint` (not 4). Mark SPEC-055 done.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- 0A: copy the atomic conditional-write from `arcgateway/pairing.py:788-794` (`UPDATE … WHERE <cond>` → `rowcount>0`); WAL + `busy_timeout` + `BEGIN IMMEDIATE` (arcstore sqlite.py:252-301). Build the generic `mutable_records` plane (SPEC-032 SDD:34-40,64-65); tasks are one collection on it.
- 0B: the gate IS the whole change — `_should_activate` before `deliver_fn` in `_handle_incoming` (capabilities.py:144-172); `_interrupt_for` still only picks steer-vs-followup once activated.

**Edge Cases:**
- 0A: the mutable plane must NOT copy arcteam/registry.py:88-100's non-atomic read-then-write; every ownership decision is a conditional write.
- 0B: DM traffic has empty mentions → must still wake the recipient (only that inbox receives it); `critical` overrides the gate.

**Performance:**
- 0A: SQLite single-writer; document (don't build) the single-write-owner proxy (SPEC-032 SDD:85). Gate the conditional write with a 100-run stress test.
- 0B: this phase IS the fleet-wide token-cost fix (one @mention → 1 run, not 4 — the ~15x anti-pattern).

**References:**
- arcgateway/pairing.py:788-794 · arcstore/backends/sqlite.py:252-301 · SPEC-032 SDD:34-40,64-65,82-85 · SPEC-055 README:19-71 · modules/messaging/capabilities.py:116-172

## Phase A — arcstore durable task directory (source of truth)
- [ ] A1 — `Task` Pydantic model (incl. `parent_id`, `run_id`, `output`, `blocked_by`) + `TaskStatus`/`Priority` enums (SDD §2). Tests: schema/validation.
- [ ] A2 — `tasks` collection on the **Phase-0A mutable plane** + migration; indexes on `status`, `owner_did`, `created_at`.
- [ ] A3 — CRUD seam: `create`, `get`, `list(scope/status/owner)`, `update`. Tests: roundtrip + list filters.
- [ ] A4 — **Atomic claim/assign** (NFR-2): `claim_next(agent_did)->(Task|None, reason)` + `assign(id,to,by)` as conditional writes. Tests: **concurrency test that forces interleaving** (two claimers, same task → exactly one owner; the other gets `no_tasks_available`/`continue_current`) — must actually interleave (Barrier/Event), not a sequential mock ([[feedback_concurrency_tests_must_interleave]]). `assign` rejects in_progress-owned-by-other.
- [ ] A5 — Audit event on every write (NFR-3). Tests: audit emitted with actor_did.

### Research Insights

**From Solutions Archive:**
- security-issues/2026-02-16-async-scheduler-hardening — "re-read from store for latest state" (never decide on stale in-memory counters) + explicit update allowlist; both apply directly to the claim/assign paths.

**Best Practices:**
- Copy the atomic single-owner claim verbatim from `arcgateway/pairing.py:788-794`: `UPDATE tasks SET owner=? WHERE id=? AND owner IS NULL` → `rowcount==1` = you won, `0` = already owned. SQLite serializes the two UPDATEs; no app lock.
- SQLite discipline: WAL + `busy_timeout=5000` + `BEGIN IMMEDIATE` (up-front write lock) — mirror arcstore/backends/sqlite.py:252-301.
- Run the continue_current / at-capacity check INSIDE the claim transaction — do NOT copy the non-atomic read-then-write at arcteam/registry.py:88-100.
- Add owner/run_id/status columns via a forward-only `_ensure_columns`/`_reconcile_columns` ALTER (arcmemory/db.py:182-197; arcstore sqlite.py:270-284).
- Do NOT reuse the scheduler's JSON read-all/write-all store for the claim — right module shape, wrong concurrency model.

**Edge Cases:**
- Two simultaneous claimers → conditional UPDATE yields exactly one `rowcount==1`; loser re-queries current owner.
- Orphaned `run_id` after a crash → lease-expiry predicate `WHERE owner IS NULL OR lease_expires_at < ?` + a soft-recovery sweep (mirror pairing TTL sweep pairing.py:796-813); never hard-delete.
- Reassigning an in_progress task must reject → add `AND status != 'in_progress'` so the UPDATE matches 0 rows.
- Idempotent re-claim by the same owner → `WHERE owner IS NULL OR owner=?` so a retry is a no-op success.

**Performance:**
- SPEC-032 mutable plane is NOT built — Phase A must land on it or build arc's first mutable table. SQLite single-writer; WAL+busy_timeout is "fine at current scale"; document (don't build) the single-write-owner proxy (SPEC-032 SDD:85). Discard event-sourced task state (SDD:84 anti-pattern). Gate with a 100-run `asyncio.gather`+`asyncio.Event` stress test.

**References:**
- arcgateway/pairing.py:788-794 (claim), :796-813 (TTL sweep) · arcstore/backends/sqlite.py:252-301,270-284,305-360 · arcmemory/db.py:182-197 · arcteam/registry.py:88-100 (avoid) · SPEC-032 SDD:19-40,82-85; arcstore/__init__.py:23-31 (only spool plane today)

## Phase B — arcagent `tasks` module (mirror scheduler)
- [ ] B1 — Scaffold `arcagent/modules/tasks/` (models/config/store/capabilities/_runtime/__init__) mirroring `modules/scheduler/`. `TasksConfig(enabled, max_active=1)`.
- [ ] B2 — `store.py` thin client over the arcstore tasks seam (no logic dup).
- [ ] B3 — Tools (SDD §3), each TDD + audit + `@handle→DID` resolve + sanitize: `create_task`, `update_task`, `start_task`, `complete_task`, `fail_task`, `assign_task`, `claim_task`, `list_tasks`, `decompose_task` (FR-15), `set_task_output` (FR-13).
- [ ] B4 — Concurrency: one `in_progress` task/agent. `claim_task`/`start_task` of a NEW *independent* task while active → `continue_current`; a task in the agent's current **dependency chain** is exempt (FR-5); `claim_next` skips tasks with unmet `blocked_by` (FR-9 ordering). Tests: cap + dependency-chain exemption + blocked-dep skip.
- [ ] B5 — Module registration + default wiring in the agent blueprint/scaffold (mirror scheduler enablement).

### Research Insights

**From Solutions Archive:**
- security-issues/2026-02-16-async-scheduler-hardening — new-module checklist: NFKC-normalize all user text before validation (LLM01 homoglyph bypass); explicit update-field allowlist (never raw kwargs, ASI02); isinstance-guard real+mock stores; `except Exception` not BaseException; bounded queue + dedup; idempotent shutdown.

**Best Practices:**
- Mirror the 7-file scheduler layout (__init__ docstring-only / models / config / store / _runtime / [engine] / capabilities). Auto-wired by signature introspection (agent_lifecycle.py:186-249) — expose `bind()`/`state()`, zero manual registration.
- Tools: `@tool`, typed signature (schema inferred), return `json.dumps({...})`, catch (ValueError,TypeError,KeyError)→{"error":...}; declare classification `read_only`|`state_modifying` honestly.
- Audit is CENTRAL — tool_registry wraps every tool + fires audit/policy keyed on classification (tool_registry.py:214-327). Do NOT emit audit inside the tool (corrects SDD §7).
- Identity: mirror the MESSAGING module's _runtime (receives identity/agent_did via configure(), stores st.identity) — scheduler's _State has none. Gate every mutation on `st.identity.did == task.owner_did`.
- `@handle→DID` via `arcteam.registry.resolve(st.registry, ref)` at the tool boundary; reject UnknownHandle fail-closed (mirror messaging). Reuse `validate_prompt` (NFKC+zero-width+injection-regex, models.py:14-72). Allowlist-drop-None update ({title,description,priority}; never id/owner/status). Enable via `[modules.tasks]` at arccli/commands/agent/_common.py:184-188.

**Edge Cases:**
- Owner-only mutation is NET-NEW logic (scheduler has no owner concept) — the single biggest deviation from the template.
- not-found → store raises KeyError → tool returns {"error":"... not found"}.
- No existing module imports arcstore — a thin arcstore-backed TaskStore diverges from the template; keep the load/add/update/get interface identical + add a `claim()` method.

**Performance:**
- Scheduler JSON store re-reads/rewrites the whole file per op (fine at max=50); an arcstore-backed indexed store avoids full rewrites at higher task volume. Cross-module signalling via the ModuleBus (schedule:completed pattern, scheduler.py:251-260), never direct imports.

**References:**
- modules/scheduler/{__init__.py, models.py:14-183, config.py:13-36, store.py:21-112, _runtime.py:40-108, capabilities.py:40-233} · core/agent_lifecycle.py:186-249 · core/tool_registry.py:214-327 · modules/messaging/capabilities.py:157 (st.identity) · arcteam/registry.py:40-74 · arccli/commands/agent/_common.py:184-188

## Phase C — cross-agent assignment (arcteam) — FR-3
- [ ] C1 — `task.assigned` signed message type (arcteam) carrying task id + summary, `mentions=[assignee_did]`. Tests: sign/verify + mentions populated.
- [ ] C2 — `assign_task` (B3) → arcstore `assign` + emit the arcteam envelope. Tests: assignment writes owner AND notifies.
- [ ] C3 — Assignee-side handler: on `task.assigned` delivery, adopt/start the task. Compose with **SPEC-055** so only the assignee wakes. Tests: assignee wakes + starts; non-assignees don't.

### Research Insights

**From Solutions Archive:**
- security-issues/2026-02-16-async-scheduler-hardening — explicit allowlist + minimal-payload discipline applies to the `task.assigned` envelope body.

**Best Practices:**
- Do NOT set `mentions=[assignee_did]` directly — `send()` calls `apply_mentions` which OVERWRITES mentions from `@handles` in the body (mentions.py:44). Put `@assignee_handle` in the body; it also auto-sets action_required + priority.
- Add the type as an enum member: `TASK_ASSIGNED="task_assigned"` in MsgType (types.py:27-35) — auto-flows through signing + both validation sites. Avoid `"task.assigned"` (dot) and the existing `TASK="task"` collision.
- Envelope = pointer (task id + terse title), authoritative read from arcstore. Write arcstore FIRST, then notify; never gate the arcstore owner-write on notify success.
- Send as a DM to `agent://assignee` — only that inbox stream receives it, so "only the assignee wakes" holds even before SPEC-055 lands. Make the assignee-side "adopt task" handler idempotent on task_id and share the claim_task path.

**Edge Cases:**
- Assignee offline → durable pull consumer redelivers on reconnect (no loss); the 300s replay-freshness window bounds notification staleness — rely on arcstore as durable truth.
- At-capacity → cap enforced in arcstore claim; envelope/handler must tolerate "already owned by me".
- Non-teammate DID → rejected at arcstore assign AND by arcteam UnknownHandle (messenger.py:296-298).
- Duplicate assignment (fresh id/nonce) → arcteam dedup won't catch; handler idempotency required.
- Decline → arcstore transition (owner=None/backlog) + reply thread; no arcteam decline primitive.
- Classification no-write-down → over-classified body to under-cleared assignee is REFUSED (messenger.py:243-267); keep body minimal.

**Performance:**
- A DM assignment is one stream + one append — cheap; one registry snapshot reused per send (messenger.py:292). Per-delivery verification does one list_entities in _verify_origin (:546) — a hot path only at high traffic, out of scope.

**References:**
- arcteam/types.py:27-35 (MsgType, existing TASK), :80-105 (Message) · mentions.py:28-49 (overwrite at :44) · messenger.py:283-390,243-267,503-557 · crypto.py:24-38,93-119 · registry.py:40-65 · SPEC-055 README (_should_activate not yet in code)

## Phase D — arcui (view + at-rest mutation + steer) — FR-6/7/8/10
- [ ] D1 — Re-point `/api/team/tasks` + `/api/agents/{id}/tasks` to arcstore rows (keep `TasksResponse`). Tests: endpoints serve arcstore data.
- [ ] D2 — Team **kanban** page (columns backlog/todo/in_progress/review/done + failed lane; cards: title/priority/owner/run-link/blocked-badge-v2). Live via existing `/ws` off arcstore (SPEC-026 — no parallel push). tsc.
- [ ] D3 — Per-agent task list in agent-detail (mirror sessions/schedules tab).
- [ ] D4 — **Operator-gated mutation routes** `POST /api/team/tasks`, `PATCH /api/tasks/{id}` — **409 if in_progress** (edit-at-rest only, FR-7); audited. Tests: operator-only, in-progress rejected, viewer read-only.
- [ ] D5 — **Steer-in-flight** (FR-8): in-progress card shows "Steer owner" → team-message composer addressed to `owner_did` (not an edit form). Task drawer mirrors `schedule-drawer.tsx`. tsc.
- [ ] D6 — Board **filters** (owner/priority/status/tag, extend tasks.tsx FilterPills) + **metrics row** (in-progress/done-today/avg time-to-done/failed) + **counts** strip (tasks/inbox/blocked/backlog) — FR-16. tsc.
- [ ] D7 — Task drawer: **activity timeline** from `observe.audit(target=task_id)` (FR-12); **run+cost link** via `run_id`→existing run/trace view (FR-11); **structured `output`** display on done (FR-13). tsc.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- Re-point the read endpoints by adding an `Observe.tasks()` reader mirroring `Observe.audit` (observe.py:207-213). `TasksResponse` is opaque list[dict] → swap the backing store without touching the schema or web client (lowest blast radius).
- Operator-gated mutation: copy files_write.py:104-198 exactly — operator gate first (403 for viewer) → guard → write → audit via `emit_mutation_audit` (audit.py:222-252). Never call audit_event directly; arcui NEVER signs (flags dependent sigs stale).
- 409-on-in_progress: after the operator gate, read the task's current status; if in_progress → `emit_mutation_audit(outcome='denied')` + `JSONResponse(ErrorResponse, status_code=409)`. Client ApiError already carries `.status` (api.ts).
- Drawer/card: mirror schedule-drawer.tsx (shared pure formatter + Field grid + status pill + JsonBlock) + an operator-only action footer; kanban columns from tasks.tsx status buckets.
- Steer-owner: open a MentionComposer pre-seeded with the owner's `@handle` and `post()` via useTeamStream into the owner's channel — reuses the existing forward path (server derives the human DID from the token), so arcui still never signs/routes.

**Edge Cases:**
- Viewer can read + post to /ws/team; every at-rest mutation must 403 at the operator gate. Decide whether "steer owner" (a message post) requires operator.
- Zero-stale WITHOUT polling: subscribe the existing `tasks:updated` fs-watcher event (fs_watcher.py:57) + FileEventBus (file_events.py, currently UNWIRED into arcui) and invalidate the ['team','tasks'] / ['agent',id,'tasks'] query keys. Do NOT reintroduce the 5s poll SPEC-026 killed.
- Optimistic card move vs 409 → react-query optimistic mutation with rollback on `ApiError.status===409`; reconcile to server truth on the next WS invalidation.
- Owner offline → an at-rest store write still succeeds (it's a store write, not an RPC); badge "owner offline — change queued" from roster live state (team_pages.py:232-234). Validate the inbound patch body with a typed Pydantic model even though the read stays opaque.

**Performance:**
- Fleet /api/team/tasks today is O(agents) sequential file reads (team_pages.py:186-190); re-pointing to one arcstore `query("tasks", ...)` collapses it to a single indexed read — the primary scalability reason to re-point. FileEventBus.emit is isolation-safe (file_events.py:83-89); auth is constant-time + bounded-LRU sessions (no new memory pressure).

**References:**
- arcui/routes/team_pages.py:183-212 · routes/agent_detail/sessions.py:143-198 · schemas.py:142-147 · routes/agent_detail/files_write.py:104-198 (mutation model) · auth.py:183-263 · audit.py:222-252 · observe.py:132-213 · routes/team_ws.py:81-129 + team_stream.py · web/src/components/{schedule-drawer.tsx:15-95, mention-composer.tsx} · web/src/pages/tasks.tsx:11-88 · web/src/lib/{queries.ts:41-59, api.ts:4-63} · arcgateway/{fs_watcher.py:57, file_events.py:42-89}

## Phase F — arccli task surface (FR-14) [arccli]
- [ ] F1 — `arc task create|list|edit|assign|complete` calling the arcstore task seam (same path as arcui); operator-gated + audited; at-rest edit only (refuse in_progress). Tests.
- [ ] F2 — `arc task talk <id>` — steer the owner via an arcteam message (not a task edit). Tests.
- [ ] F3 — ruff + mypy --strict + arccli suite.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- Reuse arccli's existing `team` command patterns (`arccli/commands/team.py`) for `@handle` resolve + signing + the `MsgType` validation site; route human CLI writes through the SAME arcstore task seam + operator gate as arcui (no parallel path).

**Edge Cases:**
- CLI operator-gating = the invoking DID must be an operator; `talk` is a message (allowed on any task), `edit` on an in_progress task is refused (parity with arcui's 409).

**Performance:**
- _(none)_

**References:**
- arccli/commands/team.py:752 (MsgType validation site) · arcui/routes/agent_detail/files_write.py:104-198 (operator-gate parity)

## Phase E — e2e + live-verify
- [ ] E1 — e2e: agent A creates+assigns a task to B → B is woken (055) → B starts→completes → both appear on the kanban with correct owners/status.
- [ ] E2 — Live on olivia: create a task from one agent, assign to another, watch it move across the kanban; confirm atomic single-ownership; confirm at-rest edit works and in-progress edit offers "steer owner". Update BACKLOG (ORCH-2 → done).
- [ ] E3 — Full gates: arcstore/arcagent/arcteam/arcui/arccli suites + ruff + mypy --strict + tsc all green; ≥80% coverage on new code; `make architecture-tests` (add `tasks` module-bus priority entry if needed).

### Research Insights

**From Solutions Archive:**
- The 2026-07-10 DGX cross-agent key-bleed → test_multi_agent_runtime_isolation.py is the pattern for turning a live DGX finding into a deterministic forced-interleave regression test.

**Best Practices:**
- Mirror tests/integration/test_spec031_e2e.py: root-level cross-package E2E; real `nats-server -js` subprocess fixture with `skipif(shutil.which('nats-server') is None)` + `@pytest.mark.slow`; stub ONLY the LLM/run seam (real arcteam/registry/crypto/NATS); subscribe-first then `asyncio.wait_for(wake_event.wait(), timeout=10)`; poll-until-deadline (never bare sleep+assert).
- Atomic-claim single-owner test: mirror the race-stress `asyncio.Event` gate (test_race_regression_stress.py) — N concurrent claim() via asyncio.gather aligned on an Event, assert exactly one rowcount==1 across 100 runs. Repo convention is asyncio.Event, NOT threading.Barrier ([[feedback_concurrency_tests_must_interleave]]).
- Coverage: the ENFORCED floor is 80 line / 75 branch per package (add the tasks module to `_COVERAGE_TARGETS`, coverage_report.py:59-64); 90% "core" is a review-time standard, not a script assertion. Mark the NATS E2E + 100-run claim-stress `@pytest.mark.slow`.

**Edge Cases:**
- Architecture guard: test_module_bus_priority_assignments.py:53-72 likely needs a `tasks` priority entry if the module subscribes to bus events; DAG guards fine if no upward imports; a new arcstore table needs no architecture-guard edit.
- Keep the LLM a scripted seam so the E2E is deterministic — cover the run-loop tool_use path (just fixed) in a SEPARATE test, not the multi-agent NATS E2E.
- Board eventual-consistency → poll the board/query until both cards present or a deadline lapses; never a fixed sleep.
- Assert per-agent isolation of task/claim state under interleave (the DGX 32-agent global-bleed risk), not just a single A→B pair.

**Performance:**
- Module-scoped nats-server fixture amortizes the ~sub-second spin — keep Phase E cases in one module. 100-run stress ≈ 5-10s; the 0.9^100≈2.6e-5 argument sets the confidence bar.

**References:**
- tests/integration/test_spec031_e2e.py:55-100,174-246,296-333,256-266 · tests/integration/test_spec054_e2e.py:3-14 · packages/arcgateway/tests/integration/test_race_regression_stress.py:8-11,58-91,123-143 · packages/arcagent/tests/security/test_multi_agent_runtime_isolation.py · Makefile:64-138 · scripts/coverage_report.py:53-64 · tests/architecture/test_module_bus_priority_assignments.py:53-72 · docs/deploy/single-node.md + packages/arcui/tests/integration/test_visible_ui_smoke.py

## Sequencing / parallelism
- **Phase 0 (0A ∥ 0B) → A → B → (C ∥ D ∥ F) → E.** 0A (arcstore mutable plane) is the foundation for A; 0B (SPEC-055) closes the assignee-wake dependency. 0A ∥ 0B (arcstore vs arcagent). C (arcteam), D (arcui), and F (arccli) run in parallel once B lands.
- **SPEC-055 is now Phase 0B (absorbed)** — no longer an external dependency.
- File-ownership partitioned by package (arcstore / arcagent / arcteam / arcui) so parallel work doesn't collide — per the Anthropic "partition by module" rule.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- SPEC-032 mutable plane is a real prerequisite for Phase A — either land it first, or scope Phase A to build arc's first mutable arcstore table (the atomic-claim primitive to copy already exists in pairing.py).
- DM-addressing the `task.assigned` envelope makes Phase C independent of SPEC-055 for the notification path; only the CHANNEL-scoped "only assignee wakes" path needs SPEC-055.

**Edge Cases:**
- SPEC-055's `_should_activate` predicate is spec-only (grep confirms no code) — treat "SPEC-055 lands first" as a HARD dependency for the channel path, SOFT for the DM path.

**Performance:**
- _(none)_

**References:**
- .claude/specs/SPEC-032-arcstore-directory/SDD.md · .claude/specs/SPEC-055-mention-scoped-activation/README.md

## Deferred hooks wired now (no rework later)
- `review` status = inert gate → **ORCH-3** completion verification.
- task→run auto-drive → **ORCH-3** coordinator.
- `blocked_by` enforcement + UI → **v2**.

### Research Insights

**From Solutions Archive:**
- _(none)_

**Best Practices:**
- Model the `review` completion gate and any task→run trigger as ModuleBus events (the scheduler.py:251-260 schedule:completed pattern), NOT direct imports — so ORCH-3's coordinator subscribes to them without a schema change.

**Edge Cases:**
- _(none)_

**Performance:**
- _(none)_

**References:**
- modules/scheduler/scheduler.py:251-260 (ModuleBus emit pattern)
