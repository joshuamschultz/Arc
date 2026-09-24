# Business reliability execution ledger

> **Superseded status notice — 2026-09-23:** Use the [business reliability consolidation handoff](business-reliability-consolidation.md) for current continuation. The keep-branches, hold-merge, and branch/worktree-resume instructions below are historical; the user's current direction is to consolidate on main. Historical evidence is retained. Root will record final merge hash, gates, and cleanup after verification; none is claimed here.

Started 2026-09-23 from local `main` at `3120aed5`. User directive: implement the full reliability and self-service program, use Sol for research/code/tests, Luna for documentation and screen/field help, and Astra for architecture, orchestration and review. This ledger records committed and in-flight work; it is not itself evidence of completion. The checklist and current coordination section are authoritative for present status; the evidence log is a historical record.

## Completion rules

Each item needs a failing regression or reproducible baseline, the actual production path fixed, fresh relevant tests/types/lint, a security abuse case where applicable, and a reviewed customer-facing state. Record exact evidence and remaining constraints. A component unit test alone does not close an end-to-end capability. Real deployment/soak evidence is tracked separately from local code completion.

No production DGX fault injection, token disclosure, or server-only patches. Deployment and credential rotation are separate explicit operations after the tested artifact is ready. A live provider outage or an external account needing reauthorization must remain a visible typed state; never bypass policy/signature/audit for availability.

Current checkpoint: main `3313b049` includes task merge `d9dba788` (candidate `4da16a52`), settings commits `0b1fbbbb` and `0a3d0613`, replay cleanup `61fbe309` (source `20254ebf`), portable installer `ae7b72e9` (merge `978b31e5`), static assets `c7e02400`, and full-repository Ruff cleanup merged in `c3593e1d`. Scoped gates pass: 171 architecture/replay tests including 8 real-wheel checks, 524 adversarial tests, 168 task/settings Python tests, 47 frontend tests, and frontend lint/build; full-repository Ruff passes. DGX remains at `fda6991f`, hash-verified over 1,327 tracked source files; runtime `0.5.0-491cd5e6` returned health HTTP 200, but no sync/load soak is recorded. Newer main changes are not deployed. Workers remain open and frozen; use the ownership/resume notes below for next-session work.

## Acceptance checklist

| Work | Required result | Current state |
|---|---|---|
| NATS/process supervision | Broker/client startup outages and later disconnects recover; readiness truthful; owned resources stop cleanly | Local fix committed `f4646536`; deployment/soak pending |
| Fleet attachment | Configured shared broker failure cannot silently isolate agents in memory; automatic verified resubscription | Local fix committed `3ec6c3dd`; DGX deployment/soak pending |
| Durable inbox/reply/handoff | Accepted work tracked through terminal result and reply; crash recovery cannot duplicate successful tool side effects | Pending |
| Chat/turn lifecycle | Bounded queue/run/provider/tool waits, cancellation, progress and reconnect reconstruction | Stream queue/cleanup and agent queue/run integration committed on main as `a43fd485`; durable accepted-run lifecycle and full reconnect reconstruction remain open |
| LLM queue | Persistent states, fair bounded admission, controlled recovery; authorized ArcUI and ArcCLI inspect/cancel/pause/configure | Queue foundation `b966a12d` and agent queue/stream integration `a43fd485` are on main. Independent 9-test focused run passed before and after cherry-pick with skill residual changes preserved. Hosted controls/API/CLI and durable accepted-run owner remain open. |
| Prompts and traces | Effective system/strategy context verified on provider-bound requests; every call correlated to session/run | Pending |
| Knowledge/memory | Write/index/provenance and cursor recovery; end-to-end grant/discover/select/approve/sync/retrieve/revoke | Partial: catalog monitor retry fixed |
| Connections | Credential lifecycle, transient reconnect, truthful per-agent searchable state and usable failure recovery | Pending |
| Workflows/pulses/schedules | Supervisors, durable claims, misfire policy and per-item failure isolation; real UI creation/execution | Host supervision committed `f4646536`; schedules/pulses/durable delivery still pending |
| Slack/email | Durable inbound acceptance/outbound delivery, bounded retries, auth renewal and no dropped/replayed effects | Pending |
| Tasks/groups | Customer can create/use after idle/restart; errors visible; correct role controls and scalable lists | Task pagination candidate `4da16a52` merged in `d9dba788`. Independent reviewer ran and approved 117 tests; separately, the author supplied real-PostgreSQL snapshot-barrier evidence (reviewer did not run PostgreSQL). Main gates: 168 task/settings Python tests and 47 frontend tests passed; frontend lint/build passed. Static assets refreshed in `c7e02400`. Group creation/restart remains open. |
| Screen performance | Fresh after idle; bounded/paginated queries and measured load budgets; one sick dependency does not block pages | Task pagination/indexing merged in `d9dba788`; production load/latency and sync-soak evidence pending |
| Tools | Production transport contract, authorization, timeout, cancellation, teardown and optional-removal tests | Pending |
| Skills import/edit | Review/sign/activate and revision edit/re-sign/reload/rollback through customer surfaces | Anchored revision and report work remains in preserved handoff `3bc942b9` on `codex/checkpoint-skills-auth-wip`; not assigned to main. Integration and verification remain pending |
| Skill improvement | Real outcome triggers with observable eligible/blocked/evaluating/promoted states and bounded spend/changes | Production tool-event payload does not match the listener's expected payload; root cause identified, fix pending |
| Images | Real upload -> verified media -> image block -> vision provider | Local regression fixed; deployed/provider smoke pending |
| HTML reports | Authorized isolated viewer, CSP/sandbox, bounded content, provenance and abuse tests | Report work remains under `arc-sol-skills`; finish assigned fixes and component/security review before treating the viewer as accepted |
| Screen and field help | Contextual help visible in every screen and form; schema-derived dynamic help, route/field coverage checks | Committed `b3983c14`: 18-route help, authored core field catalog, exact/wildcard path matching and guided controls; future controls require follow-up help |
| Paid self-service | $20 subscription -> idempotent provision -> capability readiness -> ArcUI setup/tuning, no owner routine involvement | Arc Cloud handoff `1ef0a6c` on `codex/reliability-checkout-recovery` remains incomplete pending real grant, setup, and release work; not deployable. See [cloud recovery research](business-reliability-research/cloud-recovery.md). No launch is claimed. |
| Hosted lifecycle | Immutable tested artifact, credential-safe startup, automated updates/rollback/restore and isolated tenants | Pending |
| Sustained proof | Automated crash/partition/expiry/idle tests plus staged soak and measured error/cost budgets | Pending |

## Current ownership and open sequence

- `arc-sol`: queue controls remain **HOLD MERGE** at clean WIP `16ad709f` in `/Users/joshschultz/Projects/arc/.claude/worktrees/reliability-queue-controls`, branch `codex/reliability-queue-controls`. Scoped journal paging still fetches unbounded metadata; next session needs indexed, bounded, authorized listing design. Worker stays open and frozen.
- `arc-sol-auth`: clean handoff `db4824b7` atop WIP `5361d4c1` in `/Users/joshschultz/Projects/arc/.claude/worktrees/reliability-account-authority`, branch `codex/reliability-account-authority`. See the [checkpoint](business-reliability-checkpoint.md) for preserved originals and hash references. CLI, gateway, UI, and core wiring remain deferred; no DGX configured authority is proven. Worker stays open and frozen.
- `arc-sol-tasks`: task pagination merged in `d9dba788`, including `4da16a52`; 168 task/settings Python and 47 frontend tests, frontend lint/build passed. Independent reviewer approved 117 tests; author separately supplied real-PostgreSQL snapshot-barrier evidence. Preserve task/skill API, type, observe, schema, and static asset coordination notes for future work. Worker stays open and frozen.
- `arc-sol-cloud`: recovery commit `1ef0a6c` is on Arc Cloud `main` at merge `23e7fbc`; root verified both `1ef0a6c` and original snapshot `e4529a3` reachable, with current cloud tree equal to `1ef0a6c`. The former snapshot worktree was removed; agent directory `/private/tmp/arc-cloud-reliability-checkout` was restored at `1ef0a6c`. Launch remains blocked on real grant, setup, and release. Worker stays open and frozen.
- `arc-sol-skills`: handoff `3bc942b9` above preserved `68115322` in `/Users/joshschultz/Projects/arc/.claude/worktrees/checkpoint-skills-auth-wip`, branch `codex/checkpoint-skills-auth-wip`; preserve the 66-path original snapshot and do not merge aggregate WIP. Worker stays open and frozen.
- `arc-luna`: documentation only.
- Peer agent: user-authorized broad Ruff cleanup passed and merged as `c3593e1d`; full-repository Ruff passes. User-authorized final deployment is pending; DGX evidence currently covers `fda6991f`, and newer main is not deployed. Worker stays open and frozen.

Do not reset, stash, stage all, or switch the shared main checkout. Preserve unrelated residual edits, use scoped file ownership, and serialize integration commits. `a43fd485` was deliberately isolated and independently passed without unfinished skill changes. Future integration must preserve unrelated skill residual edits; no dependency on those edits is implied. Keep the Arc Cloud checkout separate.

Resume sequence: design indexed, bounded, authorized queue journal listing and re-review before merge; complete Arc Cloud grant/setup/release work; decide and review the remaining account-authority wiring; continue skills/report work from the preserved snapshot with seam reconciliation; later verify deployment of an exact newer main artifact and run sync/load soak. Preserve the open acceptance register; the passed scoped gates do not close durable RunIntent, inbox/reply, group lifecycle, or the other pending checklist items.

## Product decisions

- User confirmed: keep `/Users/joshschultz/Projects/arc-cloud` separate and update both source repositories. Harness changes remain on `main` here; checkout/provisioning changes stay in arc-cloud.
- Existing arc-cloud site describes $20/month hosting with customer-provided model credentials. Preserve that product behavior while adding explicit quotas and spend controls; do not imply included unlimited inference.

## Evidence log

Entries below preserve point-in-time historical evidence; use the checklist and current ownership section above for current status.

- Baseline repair commits and verification are recorded in [business-reliability-program.md](business-reliability-program.md#implementation-record). They are retained, not reimplemented.
- 2026-09-23: full implementation resumed. Wave 1 delegated; no new work is marked complete yet.

- 2026-09-23 independent review: dependency-boundary/import and fleet bootstrap subset: 25 passed. Existing cross-package adversarial battery: 413 passed in 9.61s with isolated loopback access; initial sandbox run blocked a local test server bind (412 passed, one environment refusal), resolved by permitted loopback rerun. New recovery abuse cases and integrated gates remain in progress.

- 2026-09-23 `3ec6c3dd`: configured fleet no longer substitutes local memory; verified subscription activation, identity-race refusal, retry and live availability implemented. Worker evidence: 852 ArcTeam/real-NATS tests, 168 messaging tests, 431 adversarial tests; strict mypy 69 files and lint/format clean. Independent review: 46 tests including actual initial outage, broker kill/restart, signed DM/group delivery and architecture gates passed in 10.02s. Durable accepted-run/reply reconciliation remains separately open.

- 2026-09-23 `f4646536`: lifespan-owned broker supervision, bounded restart/cleanup, runner fencing/recovery, startup store/messaging/registration retry and safe capability readiness. Worker broader gates: 1296 passed, 2 skipped; adversarial 471 passed; strict types/lint clean. Independent review: 65 lifecycle/CLI/architecture tests passed; 22 follow-up cancellation/optional-import tests passed; final worker broker reaper regression suite 19 passed. Deployment template parity, fail-on-exhaustion readiness and credential-safe startup are assigned to the cloud wave, not claimed fixed here.

- 2026-09-23 UI integration in review: independent 25 DOM tests and 104 focused Python tests passed. Worker verified PostgreSQL keyset pagination/index use with 1,000 history rows and 2 real database integration tests; frontend lint/build and strict backend checks passed. Luna expanded the help catalog after this build; corrected configuration path matching and a fresh frontend build remain required before commit. These are local results, not DGX latency or sustained stability evidence.

- 2026-09-23 `b3983c14`: committed contextual screen/field help, guided settings, bounded task pages/indexes and idle refresh. Final worker and independent reviewer frontend runs: 43 tests passed; lint/TypeScript/Vite build passed and static assets rebuilt. Prior unchanged backend evidence: 104 focused Python and 2 real PostgreSQL integration tests. Luna inventory corrected to actual exact-path/wildcard resolver; authored entries do not imply every optional field is visible in every installation. Safe HTML reports and signed skill revision/import are assigned to the next Sol wave.

- 2026-09-23 `b966a12d`: committed fair bounded process-owned queue coordinator, encrypted CAS journal with independent Vault anchor/crash reconciliation, optional Vault signing/custody adapters, public adjacent-layer injection seams and abuse cases. Worker gates: ArcLLM 1492 passed/1 skipped; ArcTrust+ArcRun 1438 passed/11 skipped; adversarial 500 passed; strict types/lint clean. Independent review: 46 queue/Vault/facade tests plus 19 physical-absence/dependency-boundary tests passed. Queue metadata persistence alone is not resumable accepted work; actual hosted model composition, dynamic session/run trace identity, encrypted request/result owner, authorized API and CLI assigned to next integration wave. Vault adapters used HTTP mocks, not a real deployed Vault service.

- Follow-up review: task pagination exposed pre-existing client assumptions that all task rows were loaded. Completed dependencies beyond the current page can appear blocked, and page-local filter options/counts can omit matches. UI worker assigned failing regressions and bounded server-side filter/dependency/subtask projections; this remains open, not concealed by the passing pagination tests.

- 2026-09-23 `f30bf7a3`: committed task-local verified run context for shared cached models, protected trace correlation fields, strict finite queue settings, explicit shared-limit precedence and the adjacent ArcRun binding facade. Sol full suites: ArcLLM 1,504 passed/1 skipped; ArcRun 870 passed/11 skipped. Independent review: 36 focused tests, strict mypy over 103 source files, scoped Ruff and staged diff checks passed. ArcAgent streaming/shutdown integration remains uncommitted; hosted queue API/CLI, accepted-run persistence and deployment remain open. `8b2d5e08` separately records Luna's verified help/operating-guide corrections and current worktree ownership.

- Historical snapshot before `a43fd485`: ArcAgent integration was in review. Sol reported 867 core/integration tests and 513 adversarial tests passing, including shutdown while a stream waits for a session lock. The 9 focused queue identity/tracked-run tests later passed independently both before and after cherry-pick; the commit is now on main. Skill residual changes were preserved.

- Isolated account branch: `43a7e5d5` adds custody/session recovery changes; independent review found same-instance concurrent mutations could overwrite a successful update. Sol reproduced the race and committed `26ee1e75` with a bounded per-instance lock and regression tests. Worker evidence: 120 auth/WS tests, 501 adversarial tests, strict types and lint passed. Main integration and real production authority-factory composition remain open.

- Approved durable run/queue, skill, account and cloud trust integration decisions are preserved in [business-reliability-integration-plan.md](business-reliability-integration-plan.md). This is a design and acceptance plan, not a completion claim.

### Prior review and integration evidence (historical)

- `a43fd485` is on main for agent queue/stream integration. Independent focused verification passed 9 tests before and after cherry-pick; skill residual edits were preserved. This does not close hosted queue controls or durable RunIntent acceptance.
- Isolated auth commit `204ff830` has two actual disposable TLS Vault tests and 24 seam tests. DGX has no verified configured authority; auth merge awaits concrete production factory composition.
- Task candidate `1f8f565` remains isolated with reported 143 Python, 15 real-PostgreSQL, and 45 frontend tests. Independent review found seven findings and set **HOLD MERGE**; the author is fixing them. Details: `/private/tmp/arc-business-implementation/task-review.md`.
- Arc Cloud candidate `0380dc0` remains isolated on `codex/reliability-checkout-recovery`, with 29 tests plus lint, types, and site build reported. Independent review found six findings and set **HOLD MERGE**; the author is fixing them. See [cloud recovery research](business-reliability-research/cloud-recovery.md).
- The skill-improvement production event payload does not match the listener payload; the fix remains pending.
- Peer knowledge work remains on its separate branch/deployment under user authorization. No verified shipped commit or deployment is recorded here.
