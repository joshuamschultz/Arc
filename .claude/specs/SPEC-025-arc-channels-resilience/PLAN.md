# SPEC-025 — Arc Channels Resilience: Implementation Plan

**Status:** PENDING
**Phases:** 2 (v1.1 demo-blockers, then v1.2 production polish)
**Approval gates:** end of v1.1, end of v1.2
**Pillar trace:** every task lists its primary pillar(s) — Simplicity (S), Modularity (M), Security (Sec), Scalability (Sc).
**Module discipline:** tasks do not cross module boundaries. A task that touches both `arcgateway` and `arcui` is split.

---

## Phase 1 — v1.1 Demo-blocker fixes (target: 1 working week)

Stops the failure modes the operator hit on 2026-05-05. After this phase, a demo can run on AWS without the chat path silently stalling and with a Slack fallback if arcui hiccups.

### Track A — Sequence-gap detection (FR-1)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| A1 | Write failing unit test: `WebPlatformAdapter._emit` increments per-`chat_id` `seq` and writes to a 50-frame ring buffer | `arcgateway` | S, Sec | `tests/unit/test_web_adapter.py::test_send_stamps_monotonic_seq_per_chat_id` (+ 6 sibling tests) | [x] |
| A2 | Implement `_seq_counters` + `_replay_buffers` in `web.py`; modify `_emit` (or equivalent) to stamp `seq` and append to ring | `arcgateway` | S | `_stamp_and_record` in web.py — A1 passes | [x] |
| A3 | Failing test: `register_socket` with `since_seq=N` replays missed frames; ring overrun emits `recovery_banner` | `arcgateway` | Sec | `tests/unit/test_web_adapter.py::test_register_with_since_seq_replays_missed_frames` + `test_replay_emits_recovery_banner_when_ring_overran` | [x] |
| A4 | Implement replay logic in `register_socket` | `arcgateway` | S | `_replay_to_socket` in web.py — A3 passes | [x] |
| A5 | Update `chat_ws.py` route to accept and forward `?since_seq=N` query param | `arcui` | M | `tests/integration/test_chat_ws.py::test_since_seq_query_param_forwarded_to_adapter` (+ 3 siblings) | [x] |
| A6 | Failing browser test: `messages-page.js` detects gap (mock WS pushes seq=1, seq=3) and reconnects with `since_seq=1` | `arcui` | S | _deferred to W5 manual rehearsal — Playwright fixture infrastructure not yet present_ | [ ] |
| A7 | Implement `lastSeq` tracking + gap detection + exp-backoff reconnect in `messages-page.js` | `arcui` | S | code in messages-page.js (lines 30-65, 295-380, 410-440); node syntax-check passes | [x] |
| A8 | Render recovery banner when `recovery_banner` frame arrives | `arcui` | Sec | code in messages-page.js (lines 432-446); recovery banner uses existing `state.localMessages` rendering path | [x] |
| A9 | Manual rehearsal: connect to demo VM, kill caddy for 5s, restore, verify chat resumes within 2s with no operator action | (deploy) | S, Sc | manual — requires deploy + browser test | [ ] |

**Phase A acceptance:** AC-1.1, AC-1.2, AC-1.3 pass.

### Track B — Slack adapter wired for demo agents (FR-2)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| B1 | Provision Slack workspace; create bot user; collect bot token + app token | (ops) | — | manual — runbook added in DEPLOY.md §Step 4.5 | [ ] |
| B2 | Add `[platforms.slack]` block to `team/scap_isso_agent/arcagent.toml` | (config) | M | bootstrap loads — TOML parses, keys match `SlackPlatformConfig` | [x] |
| B3 | Same for `team/nlit_cora_agent/arcagent.toml` and `team/nlit_soc_agent/arcagent.toml` | (config) | M | bootstrap loads — both files updated identically | [x] |
| B4 | Add `SLACK_BOT_TOKEN` + `SLACK_APP_TOKEN` to demo VM `.env` (chmod 600) | (ops) | Sec | manual — runbook added in DEPLOY.md §Step 4.5 | [ ] |
| B5 | Failing integration test: `WebPlatformAdapter` + `SlackAdapter` both register for `scap_isso_agent`; sending via Slack emits audit with `platform="slack"`; sending via arcui emits `platform="web"` for same `(user_did, agent_did)` | `arcgateway` | M, Sec | `tests/integration/test_dual_adapter_chat.py` (3 tests) | [x] |
| B6 | Verify the test passes with no code changes (since both adapters already exist) | `arcgateway` | M | 3/3 pass; no adapter code changed | [x] |
| B7 | Update `deploy/aws/DEPLOY.md` with the Slack token setup checklist | (docs) | — | DEPLOY.md §Step 4.5 added (66 lines) | [x] |
| B8 | Manual rehearsal: kill arcui process; verify `@scap_isso` in Slack still responds and the audit chain shows `platform="slack"` | (deploy) | M, Sec | manual — pending Slack workspace + VM access | [ ] |

**Phase B acceptance:** AC-2.1, AC-2.2, AC-2.3 pass.

### Track C — Per-host deploy manifest (FR-4)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| C1 | Create `deploy/aws/agents.enabled` with the 3 demo agents | `deploy` | S | shellcheck — file created with C5 runbook embedded | [x] |
| C2 | Modify `deploy/aws/setup-vm.sh` to read manifest, rsync only listed agents, remove unlisted ones | `deploy` | S | bash -n + shellcheck (2 pre-existing warnings, 0 new); `_read_agent_manifest` shell function added | [x] |
| C3 | Modify `scripts/arc-stack.sh` (the per-agent loop) so unit health = `pass if ≥1 agent connects`, not `pass if all`; persist per-agent state to `~/.arcagent/agent-state.json` | `deploy` | M, Sc | bash -n + shellcheck clean | [x] |
| C4 | Per-agent `degraded` state surfaced in roster endpoint when an agent is in manifest but failed to connect | `arcui` | M | `tests/test_team_aggregations.py::TestRosterDegradedState` — 4/4 tests pass | [x] |
| C5 | Manual rehearsal: deliberately corrupt one agent's identity key; redeploy; verify systemd unit is `active` and roster shows that agent as `degraded` | (deploy) | M | manual — runbook embedded in `agents.enabled` comments | [ ] |

**Phase C acceptance:** AC-4.1, AC-4.2, AC-4.3 pass.

### Track D — Service worker (FR-3)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| D1 | Static-validation test for `sw.js` cache rules (no vitest infra in repo — Python static-file test instead) | `arcui` | S | `tests/unit/test_sw_static.py` — 10/10 pass | [x] |
| D2 | Implement `packages/arcui/src/arcui/static/sw.js` per SDD §C3 | `arcui` | S | 75 LOC, 2.6KB (under 5KB NFR-2); install/activate/fetch handlers all present | [x] |
| D3 | Register SW from the existing inline `<script>` in `index.html` (no `main.js` exists) — HTTPS/localhost guard, escape-hatch check | `arcui` | S | inline registration block at index.html:2069-2084 | [x] |
| D4 | Add `__SW_DISABLE__` escape hatch | `arcui` | S | guard added in registration block | [x] |
| D5 | Playwright e2e: open page, take page offline, reload; verify the shell HTML + assets render from cache | `arcui` | S | _deferred — Playwright fixtures not in repo_ | [ ] |
| D6 | Manual rehearsal: kill caddy for 3s during demo; verify operator does not see a blank page | (deploy) | S | manual — pending VM deploy | [ ] |

**Phase D acceptance:** AC-3.1, AC-3.2 pass.

### Phase 1 wrap

| # | Task | Done |
|---|---|---|
| W1 | All v1.1 tracks (A, B, C, D) acceptance criteria green | [ ] |
| W2 | `/coverage` reports ≥80% on changed files; ≥90% on new core code | [ ] |
| W3 | `ruff check` and `mypy --strict` clean | [ ] |
| W4 | `/review SPEC-025` runs through quality gates and ADR generation | [ ] |
| W5 | Tag v1.1, deploy to AWS demo VM, run a 30-minute live demo to validate | [ ] |
| W6 | If demo runs clean — proceed to Phase 2. If not — `/debug` whatever surfaced | [ ] |

**Approval gate after W6.** Operator signs off on demo reliability before Phase 2 starts.

---

## Phase 2 — v1.2 Production polish (target: 3–4 working weeks)

Items #5–#8 from the brainstorm deepening's build-order delta. Not demo-blocking; quality-of-life and federal-tier unblocks.

### Track E — Single-transport dashboard (FR-5)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| E1 | Failing test: `DashboardEventBus.subscribe()` replays last value; `publish()` distributes to all subscribers; queue-full drops oldest | `arcgateway` | S, Sc | `tests/unit/telemetry/test_dashboard_bus.py` | [ ] |
| E2 | Implement `arcgateway/telemetry/dashboard_events.py` per SDD §C5 | `arcgateway` | S | E1 passes | [ ] |
| E3 | Wire `dashboard_bus` into `EmbeddedGateway` named tuple from `bootstrap.build_for_embedded()` | `arcgateway` | M | bootstrap test | [ ] |
| E4 | Modify each existing aggregator (queue depth, circuit breakers, budget, performance, cost-efficiency, schedule history, stats, timeseries, roster) to call `dashboard_bus.publish(topic, payload)` on state change | `arcgateway` | M | per-aggregator unit tests | [ ] |
| E5 | Failing route test: `/ws/dashboard` accepts `subscribe` frame, replays last values, pushes new ones | `arcui` | S | `tests/integration/test_dashboard_ws.py` | [ ] |
| E6 | Implement `arcui/routes/dashboard_ws.py` per SDD §C5 | `arcui` | S | E5 passes | [ ] |
| E7 | Refactor `dashboard-page.js` (existing, currently uses `setInterval`) to open one WS, subscribe to topics, render on push | `arcui` | S | unit + e2e | [ ] |
| E8 | Add `ARCUI_LEGACY_POLLING` feature flag — when false, polling endpoints return 410 Gone; when true (default for v1.2), they keep working | `arcui` | M | route test | [ ] |
| E9 | Document the migration in `packages/arcui/MIGRATION-v1.2.md` for any external consumer (CLI, MCP, etc.) | (docs) | — | doc review | [ ] |
| E10 | Manual rehearsal: 1-hour idle dashboard session — Caddy access log shows ≤5 GET/sec total | (deploy) | S, Sc | manual log inspection | [ ] |

**Phase E acceptance:** AC-5.1, AC-5.2 pass.

### Track F — Mattermost adapter (FR-6)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| F1 | Stand up `docker-compose.mattermost.yml` for testing (MM Team Edition + Postgres) | (test fixtures) | — | docker-compose up | [ ] |
| F2 | Failing contract test: `MattermostAdapter` implements every method in `BasePlatformAdapter` Protocol | `arcgateway` | M | `tests/unit/adapters/test_mattermost_contract.py` | [ ] |
| F3 | Implement `packages/arcgateway/src/arcgateway/adapters/mattermost.py` per SDD §C6 — connect, ingest_event, send, dispatch_delta, audit emission, per-socket queue | `arcgateway` | M, Sec, Sc | F2 passes | [ ] |
| F4 | Reuse the per-socket bounded-queue + drain-task pattern from `web.py` (extract to a base mixin if duplication justifies — apply DRY only after 3rd instance, per project rules) | `arcgateway` | S, M | refactor + tests | [ ] |
| F5 | Federal-tier guard: at `tier=federal`, `MattermostAdapter.__init__` validates `server_url` is RFC1918 / loopback / configured intranet domain; otherwise raises | `arcgateway` | Sec | unit test | [ ] |
| F6 | Integration test: send a message via the docker MM instance, assert the agent runs and replies; assert audit chain has `platform="mattermost"` | `arcgateway` | M, Sec | `tests/integration/test_mattermost_adapter.py` | [ ] |
| F7 | Deploy fixture: example `[platforms.mattermost]` block in a sample agent config | (docs) | — | doc review | [ ] |
| F8 | Manual rehearsal: against a real on-prem MM server (Sandia mock), verify `@scap_isso` responds | (ops) | M | manual | [ ] |

**Phase F acceptance:** AC-6.1, AC-6.2, AC-6.3 pass.

### Track G — OS-user binding in audit (FR-7)

| # | Task | Module | Pillar | Test | Done |
|---|---|---|---|---|---|
| G1 | Failing test: `SessionStartFields.from_token()` populates `uid` and `username` from `pwd.getpwuid(os.getuid())` | `arcui` | Sec | `tests/unit/test_auth_session_start.py` | [ ] |
| G2 | Modify `arcui/auth.py:SessionStartFields` per SDD §C7; handle Windows `ImportError` gracefully | `arcui` | Sec | G1 passes | [ ] |
| G3 | Audit emission test: `gateway.session.start` audit event includes `uid` and `username` keys | `arcgateway` (audit) | Sec | audit JSONL grep test | [ ] |
| G4 | Update `arcgateway/audit.py` (or wherever the start event schema lives) to validate the new fields | `arcgateway` | Sec | schema test | [ ] |
| G5 | Federal compliance check: deploy v1.2 to a federal tier fixture, run a session, verify audit includes the OS user; map to NIST AU-3 | (compliance docs) | Sec | manual + audit grep | [ ] |

**Phase G acceptance:** AC-7.1 passes; FedRAMP Low gate clears.

### Phase 2 wrap

| # | Task | Done |
|---|---|---|
| X1 | All v1.2 tracks (E, F, G) acceptance criteria green | [ ] |
| X2 | `/coverage` reports ≥80% on changed files; ≥90% on new core code | [ ] |
| X3 | `ruff check` and `mypy --strict` clean across the changed surface | [ ] |
| X4 | `/review SPEC-025` rerun for the v1.2 surface | [ ] |
| X5 | Tag v1.2 | [ ] |
| X6 | Update `.claude/solutions/` with any compounding lessons (`/compound`) | [ ] |
| X7 | Demote SPEC-025 status to `complete`; populate README's `## Learnings` | [ ] |

**Approval gate after X7.** Spec closes; SPEC-026 follow-up if Teams adapter or Lit refactor are scheduled.

---

## Cross-cutting work (non-track)

### Documentation
- `packages/arcui/MIGRATION-v1.2.md` — for external consumers of the polling endpoints
- `deploy/aws/DEPLOY.md` — Slack + Mattermost section additions
- `.claude/decisions-log.md` — D-NNN entries for each Decision in README.md

### Monitoring (post-deploy)
- New Grafana panel: `dashboard_ws_active_subscribers` — proves the migration off polling worked
- New Grafana panel: `web_seq_gaps_per_minute` — alerts if gap rate spikes (network instability, bug in the seq logic, etc.)
- New audit query: count `platform="slack"` and `platform="mattermost"` events per agent — proves the fallback channels are exercised

### Risk monitoring
- `seq` ring buffer overflow rate — alert if any chat has >5% recovery banners (means ring is too small or reconnect storm)
- Service worker cache version churn — every deploy bumps `CACHE_VERSION`; track via UA-string log

---

## Counts
- **Tasks (Phase 1):** 28 across 4 tracks
- **Tasks (Phase 2):** 25 across 3 tracks
- **Total:** 53 tasks
- **Acceptance criteria covered:** AC-1.1 through AC-7.1 (15 in PRD)
- **New files:** 4 (`sw.js`, `dashboard_events.py`, `dashboard_ws.py`, `mattermost.py`) + 1 manifest (`agents.enabled`)
- **Modified files:** 7 (web.py, chat_ws.py, messages-page.js, main.js, setup-vm.sh, arc-stack.sh, auth.py)

## Reset on failure

If Phase 1 demo rehearsal (W5) fails:
1. STOP — do not proceed to Phase 2
2. `/debug` the specific failure
3. Update PLAN.md with new tasks
4. Re-run W5 before approval gate

If Phase 2 acceptance fails (e.g. coverage drop, audit regression):
1. Roll back the v1.2 tag
2. Identify the offending track
3. Add remediation tasks to PLAN.md
4. Do not declare Phase 2 complete until X7 is green
