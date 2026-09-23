# Business reliability execution ledger

Started 2026-09-23 from local `main` at `3120aed5`. User directive: implement the full reliability and self-service program, use Sol for research/code/tests, Luna for documentation and screen/field help, and Astra for architecture, orchestration and review. All harness changes are made and committed in this checkout. Do not mistake this ledger or a research report for completed implementation.

## Completion rules

Each item needs a failing regression or reproducible baseline, the actual production path fixed, fresh relevant tests/types/lint, a security abuse case where applicable, and a reviewed customer-facing state. Record exact evidence and remaining constraints. A component unit test alone does not close an end-to-end capability. Real deployment/soak evidence is tracked separately from local code completion.

No production DGX fault injection, token disclosure, or server-only patches. Deployment and credential rotation are separate explicit operations after the tested artifact is ready. A live provider outage or an external account needing reauthorization must remain a visible typed state; never bypass policy/signature/audit for availability.

## Acceptance checklist

| Work | Required result | Current state |
|---|---|---|
| NATS/process supervision | Broker/client startup outages and later disconnects recover; readiness truthful; owned resources stop cleanly | Local fix committed `f4646536`; deployment/soak pending |
| Fleet attachment | Configured shared broker failure cannot silently isolate agents in memory; automatic verified resubscription | Local fix committed `3ec6c3dd`; DGX deployment/soak pending |
| Durable inbox/reply/handoff | Accepted work tracked through terminal result and reply; crash recovery cannot duplicate successful tool side effects | Pending |
| Chat/turn lifecycle | Bounded queue/run/provider/tool waits, cancellation, progress and reconnect reconstruction | Partial: stream queue/cleanup fixed |
| LLM queue | Persistent states, fair bounded admission, controlled recovery; authorized ArcUI and ArcCLI inspect/cancel/pause/configure | Foundation committed `b966a12d`; Sol implementing actual hosted owner, trace context, authorized API/CLI and accepted-run persistence |
| Prompts and traces | Effective system/strategy context verified on provider-bound requests; every call correlated to session/run | Pending |
| Knowledge/memory | Write/index/provenance and cursor recovery; end-to-end grant/discover/select/approve/sync/retrieve/revoke | Partial: catalog monitor retry fixed |
| Connections | Credential lifecycle, transient reconnect, truthful per-agent searchable state and usable failure recovery | Pending |
| Workflows/pulses/schedules | Supervisors, durable claims, misfire policy and per-item failure isolation; real UI creation/execution | Host supervision committed `f4646536`; schedules/pulses/durable delivery still pending |
| Slack/email | Durable inbound acceptance/outbound delivery, bounded retries, auth renewal and no dropped/replayed effects | Pending |
| Tasks/groups | Customer can create/use after idle/restart; errors visible; correct role controls and scalable lists | Bounded task listing and refresh committed `b3983c14`; group creation and full restart journey still open |
| Screen performance | Fresh after idle; bounded/paginated queries and measured load budgets; one sick dependency does not block pages | Task pagination/indexing and idle refresh committed `b3983c14`; production load/latency evidence pending |
| Tools | Production transport contract, authorization, timeout, cancellation, teardown and optional-removal tests | Pending |
| Skills import/edit | Review/sign/activate and revision edit/re-sign/reload/rollback through customer surfaces | Pending |
| Skill improvement | Real outcome triggers with observable eligible/blocked/evaluating/promoted states and bounded spend/changes | Pending |
| Images | Real upload -> verified media -> image block -> vision provider | Local regression fixed; deployed/provider smoke pending |
| HTML reports | Authorized isolated viewer, CSP/sandbox, bounded content, provenance and abuse tests | Pending |
| Screen and field help | Contextual help visible in every screen and form; schema-derived dynamic help, route/field coverage checks | Committed `b3983c14`: 18-route help, authored core field catalog, exact/wildcard path matching and guided controls; future controls require follow-up help |
| Paid self-service | $20 subscription -> idempotent provision -> capability readiness -> ArcUI setup/tuning, no owner routine involvement | Sol implementing separate arc-cloud checkout/provisioning and secure ArcUI claim; full initializer and real platform configuration open |
| Hosted lifecycle | Immutable tested artifact, credential-safe startup, automated updates/rollback/restore and isolated tenants | Pending |
| Sustained proof | Automated crash/partition/expiry/idle tests plus staged soak and measured error/cost budgets | Pending |

## Active ownership and sequence

Current wave: Sol actual hosted queue/run ownership, prompt/trace identity and queue API/CLI; Sol separate arc-cloud checkout/provisioning plus canonical account/bootstrap integration; Sol signed skill import/revision and safe HTML reports. Fleet, supervision, initial screen help/task performance, and queue foundation are committed. Source ownership is disjoint; cloud owns arctrust root exports, skill/UI worker owns arcagent root exports and the sanitizer dependency lock update. Handoffs and collision manifests are under `/tmp/arc-business-implementation/`.

Next waves: integrate help and performance fixes while implementing the durable queue seam; then durable inbox/channel delivery and scheduled execution; then prompt/trace and skill lifecycle correctness; then complete knowledge recovery and self-service provisioning. Integrate and run cross-package/security gates between waves. A wave ending does not end the full task.

## Product decisions

- User confirmed: keep `/Users/joshschultz/Projects/arc-cloud` separate and update both source repositories. Harness changes remain on `main` here; checkout/provisioning changes stay in arc-cloud.
- Existing arc-cloud site describes $20/month hosting with customer-provided model credentials. Preserve that product behavior while adding explicit quotas and spend controls; do not imply included unlimited inference.

## Evidence log

- Baseline repair commits and verification are recorded in [business-reliability-program.md](business-reliability-program.md#implementation-record). They are retained, not reimplemented.
- 2026-09-23: full implementation resumed. Wave 1 delegated; no new work is marked complete yet.

- 2026-09-23 independent review: dependency-boundary/import and fleet bootstrap subset: 25 passed. Existing cross-package adversarial battery: 413 passed in 9.61s with isolated loopback access; initial sandbox run blocked a local test server bind (412 passed, one environment refusal), resolved by permitted loopback rerun. New recovery abuse cases and integrated gates remain in progress.

- 2026-09-23 `3ec6c3dd`: configured fleet no longer substitutes local memory; verified subscription activation, identity-race refusal, retry and live availability implemented. Worker evidence: 852 ArcTeam/real-NATS tests, 168 messaging tests, 431 adversarial tests; strict mypy 69 files and lint/format clean. Independent review: 46 tests including actual initial outage, broker kill/restart, signed DM/group delivery and architecture gates passed in 10.02s. Durable accepted-run/reply reconciliation remains separately open.

- 2026-09-23 `f4646536`: lifespan-owned broker supervision, bounded restart/cleanup, runner fencing/recovery, startup store/messaging/registration retry and safe capability readiness. Worker broader gates: 1296 passed, 2 skipped; adversarial 471 passed; strict types/lint clean. Independent review: 65 lifecycle/CLI/architecture tests passed; 22 follow-up cancellation/optional-import tests passed; final worker broker reaper regression suite 19 passed. Deployment template parity, fail-on-exhaustion readiness and credential-safe startup are assigned to the cloud wave, not claimed fixed here.

- 2026-09-23 UI integration in review: independent 25 DOM tests and 104 focused Python tests passed. Worker verified PostgreSQL keyset pagination/index use with 1,000 history rows and 2 real database integration tests; frontend lint/build and strict backend checks passed. Luna expanded the help catalog after this build; corrected configuration path matching and a fresh frontend build remain required before commit. These are local results, not DGX latency or sustained stability evidence.

- 2026-09-23 `b3983c14`: committed contextual screen/field help, guided settings, bounded task pages/indexes and idle refresh. Final worker and independent reviewer frontend runs: 43 tests passed; lint/TypeScript/Vite build passed and static assets rebuilt. Prior unchanged backend evidence: 104 focused Python and 2 real PostgreSQL integration tests. Luna inventory corrected to actual exact-path/wildcard resolver; authored entries do not imply every optional field is visible in every installation. Safe HTML reports and signed skill revision/import are assigned to the next Sol wave.

- 2026-09-23 `b966a12d`: committed fair bounded process-owned queue coordinator, encrypted CAS journal with independent Vault anchor/crash reconciliation, optional Vault signing/custody adapters, public adjacent-layer injection seams and abuse cases. Worker gates: ArcLLM 1492 passed/1 skipped; ArcTrust+ArcRun 1438 passed/11 skipped; adversarial 500 passed; strict types/lint clean. Independent review: 46 queue/Vault/facade tests plus 19 physical-absence/dependency-boundary tests passed. Queue metadata persistence alone is not resumable accepted work; actual hosted model composition, dynamic session/run trace identity, encrypted request/result owner, authorized API and CLI assigned to next integration wave. Vault adapters used HTTP mocks, not a real deployed Vault service.
