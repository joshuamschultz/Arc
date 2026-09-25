# Business reliability acceptance register — 2026-09-25

This is the current product acceptance register for the reliability and self-service request. It preserves the requests in the [business reliability program](business-reliability-program.md), [execution ledger](business-reliability-execution.md), [integration plan](business-reliability-integration-plan.md), and [consolidation handoff](business-reliability-consolidation.md). It supersedes older scoped test lists as a statement of what the product must do; those runs remain evidence for only the exact code paths they exercised.

## Acceptance rules

- Close a row only with a customer-path test against the actual composition, relevant failure and abuse cases, audit/state evidence, and exact source revision. Record command, environment/dependencies, result, and limits in the execution ledger.
- A passing helper or package suite is supporting evidence, not product acceptance. An `unavailable` result is safe behavior but means the capability remains open.
- Local completion, deployment, and sustained-operation evidence are separate states. No HTTP 200, local test, or build status implies that a hosted customer can complete the journey.
- Preserve the package boundaries and fail-closed behavior in [AGENTS.md](../../AGENTS.md). Do not use memory fallbacks, unsigned artifacts, unbounded queues, or silent success to mask absent dependencies.

Latest root-reported scoped evidence: 107 Python and 161 web queue/setup/help tests passed; hosted rekey commit `0a06bcef` has 16 reported passing tests. The broad run is not green (35 failed, 2,211 passed, 5 skipped, 44 errors); reported errors include loopback sandbox restrictions and real-trust/skill-fixture policy mismatches assigned to Sol. Do not treat scoped passes as full-gate completion.

## Customer capability register

| Capability / original request | Acceptance outcome | Exact current entry points and focused evidence | Current gap / dependency |
|---|---|---|---|
| NATS supervision and reconnect | When NATS is absent at startup or disconnects later, the service reports the capability accurately, reconnects within a bounded interval, resubscribes without agent restart, and stops owned resources cleanly. Signed direct and group delivery resumes; configured messaging never silently degrades to local memory. | `packages/arcteam/src/arcteam/backends/nats.py`, `packages/arcteam/src/arcteam/team.py`, `packages/arcagent/src/arcagent/modules/messaging/`; tests `packages/arcagent/tests/unit/modules/messaging/test_reconnect_real_nats.py`, `packages/arcteam/tests/integration/test_nats_backend_server.py`, `packages/arcteam/tests/unit/test_nats_server.py`. | Local supervision/attachment fixes are recorded at `f4646536` / `3ec6c3dd`; verify current-tree composition and deployment recovery/soak. NATS being alive during the earlier DGX incident did not identify the incident cause. |
| Memory and Knowledge saving/recovery | A granted source can be discovered, selected, mapping-approved, synced, durably indexed and retrieved with provenance. Restart, transient source/store failure, reindex and revoke preserve committed data, resume safely, and revoke cached retrieval immediately. Distinguish failed writes, missing records, stale indexes, denial, empty results and UI visibility. | `packages/arcagent/src/arcagent/modules/connected_data/`, `packages/arcmemory/src/arcmemory/`, `packages/arcstore/src/arcstore/`; tests `packages/arcagent/tests/modules/connected_data/test_ingest_connection_lifecycle.py`, `packages/arcagent/tests/modules/connected_data/test_terminal_failure_escalation.py`, `packages/arcagent/tests/e2e/test_memory_promotion_e2e.py`, `packages/arcmemory/tests/`. | Catalog retry is a partial fix. Exercise real granted-connection-to-searchable-knowledge and fault/restart/revoke paths. Automatic memory promotion remains unavailable without verified score grants, trusted source and ArcTeam attachment; shared-knowledge entity promotion remains unavailable pending signed contributor/provenance contract. |
| Fast, fresh screen loads | Every supported screen stays usable with bounded queries and payloads; long histories/lists paginate; idle tabs refresh; one unavailable dependency does not block unrelated screens. Measure defined warm/cold p95 budgets under declared load. | UI router `packages/arcui/web/src/app/router.tsx`; query/API layer `packages/arcui/web/src/lib/queries.ts`, `packages/arcui/src/arcui/routes/`; `packages/arcui/web/src/pages/tasks.test.tsx` and route-specific tests. | Task paging/index work has scoped tests, but product-wide latency, idle/reconnect freshness and cross-screen dependency isolation need browser journeys and measured deployment/load evidence. |
| Chat, bounded turns and reconnect | Submit a turn and observe queued/running/progress/terminal state; provider, tool, queue and run waits have deadlines; cancellation and shutdown release capacity; reconnect reconstructs the correct session/run state without duplicate effects. | `packages/arcui/web/src/hooks/use-chat.ts`, `packages/arcagent/src/arcagent/core/agent.py`, `packages/arcrun/src/arcrun/`; tests `packages/arcagent/tests/integration/test_queue_run_identity.py`, `packages/arcllm/tests/test_queue.py`, `packages/arcrun/tests/test_p0_reliability.py`. | Stream admission and ArcAgent queue identity have local integration evidence. Durable accepted-run ownership, crash recovery, full reconnect reconstruction and uncertainty handling remain open; see RunIntent criteria in the integration plan. |
| Inbox reply and handoff | Acknowledgement follows durable work acceptance. Inbox items expose accepted/running/terminal and reply-delivery state; crash/retry cannot rerun successful tools merely because reply delivery failed. Handoff has an authorized recipient and recoverable ownership. | ArcTeam inbox/backend and ArcAgent messaging modules; `packages/arcagent/tests/` messaging integration; `packages/arcteam/tests/` messaging/inbox tests. | Durable accepted-run and inbox ownership/reconciliation and terminal customer-visible status remain in progress. The `b122761b` workflow suite includes a regression proving that a lost database response after task materialization followed by runner restart does not duplicate deterministic task IDs; this is scoped evidence, not full task lifecycle acceptance. Require crash injection at acceptance, run completion, outbound send and acknowledgement boundaries. |
| Model-call queue in UI and CLI | Hosted calls use one durable, bounded, fair coordinator shared by model, API/UI and CLI. Authorized operators can filter/list metadata, see wait reason/age, cancel with requested-versus-confirmed status, pause/resume and change approved limits. Tenant isolation, pagination and cancellation uncertainty are explicit. | ArcLLM `packages/arcllm/src/arcllm/queue_control.py`, `queue_journal.py`; ArcAgent injection in `packages/arcagent/src/arcagent/core/agent.py`; ArcUI `packages/arcui/src/arcui/`; CLI `packages/arccli/src/arccli/`; tests `packages/arcllm/tests/test_queue_control.py`, `test_queue.py`, `packages/arcagent/tests/integration/test_queue_run_identity.py`. | Queue surface is committed at `8b03638b` (page, router/nav, server API, CLI, help, tests and built assets). Root reports 107 Python and 161 web queue/setup/help tests passing. UI polls every five seconds, shows age and requested-versus-confirmed cancellation, and uses revision controls. CLI `arc queue --email ACCOUNT jobs|status|pause|resume|limits|cancel` prompts for the password instead of putting it in argv; `/api/queue` requires a live operator session. `factory.tenant_scope` and `queue_tenant_id` must be composed or readiness fails closed. Format-4 fake-anchor 10k measurements remain 26.15 seconds admission, 75,436 nodes/66.45 MB, 0.20 seconds reopen and 11.29 seconds recovery/100 CAS. Runtime controls, real broker integration and production recovery/performance remain under verification; no product acceptance is claimed. |
| Traces, sessions, effective prompts and strategy prompts | One protected correlation joins session, run, prompt versions/hashes, selected strategy, queue call, tool events and delivered response. Verify the actual provider-bound request includes effective system and strategy context. Authorized reads/export are audited and encrypted; safe IDs remain visible when payload access is denied. | ArcLLM trace capture/store `packages/arcllm/src/arcllm/`; ArcAgent prompt/session wiring `packages/arcagent/src/arcagent/`; tests `packages/arcllm/tests/test_trace_integration.py`, `test_trace_store.py`, `packages/arcagent/tests/integration/test_prompt_overlay_wiring.py`, `test_arcskill_prompt_overlay.py`, `packages/arcagent/tests/security/test_prompt_overlay_isolation.py`. | Correlation change `6f5f0944` is committed. Production end-to-end validation of the joins and provider-bound payload/version remains open; isolated trace/prompt tests do not establish those production paths. Additional root-cause gap: `packages/arcagent/src/arcagent/core/model_manager.py` swallows local checkpoint-anchor failure and non-federal witness failure; `packages/arcagent/tests/unit/core/test_spec053_hardening.py::test_nonfederal_witness_submit_failure_is_swallowed` encodes that behavior. Current `AGENTS.md` requires audit/integrity failures to fail closed at every tier; fix and replace that behavior/test before accepting trace integrity. |
| Trace checkpoint integrity | Every trace checkpoint is operator-signed and locally anchored; required external witness and local audit failures stop protected trace persistence at every tier, with refusal audited and no success-shaped checkpoint. | `packages/arcagent/src/arcagent/core/model_manager.py`; tests `packages/arcagent/tests/unit/core/test_checkpoint_sink.py`, `packages/arcagent/tests/unit/core/test_spec053_hardening.py`. | Current `_anchor_local` swallows local WORM/audit failure and `_submit_witness` swallows witness failure below federal. `test_nonfederal_witness_submit_failure_is_swallowed` asserts the behavior. This conflicts with the current all-tier audit/integrity fail-closed rule in `AGENTS.md`; root-cause fix and replacement abuse tests are required before acceptance. |
| Tools and optional capabilities | Every shipped transport has discovery, identity/policy authorization, signature verification, validation, bounded timeout, cancellation, audit and complete teardown. Removing an optional tool/extension leaves startup and unrelated tools working with typed unavailable state. | ArcAgent registry/capabilities `packages/arcagent/src/arcagent/`; architecture and security tests under `packages/arcagent/tests/architecture/`, `tests/run_adversarial_tests.py`. | Current tests cover individual transports/contracts; acceptance needs a matrix for every shipped real transport, physically absent optional components and end-to-end customer dispatch. No tool success is implied by registry discovery alone. |
| Skills: import, edit, review, improvement | Customer can import/edit through signed immutable bundles, review before activation, reload the exact verified bytes and perform authorized rollback as a new anchored activation. The live skill drawer has Body/Versions plus Edit/Save and version/rollback controls. Real successful/failed tool outcomes reach the evaluator through a typed bridge; UI shows eligible/blocked/evaluating/promoted with reason, budget and persisted status. Outcome alone never authorizes promotion. | ArcUI `packages/arcui/web/src/pages/tools-skills.tsx`, `components/skill-drawer.tsx`; ArcAgent skill lifecycle and hooks; tests `packages/arcui/web/src/components/skill-drawer.test.tsx`, `packages/arcagent/tests/integration/test_arcskill_prompt_overlay.py`, capability security tests under `packages/arcagent/tests/security/capabilities/`. | UI controls exist; production signer/anchor factory, authenticated startup wiring and full customer activation/recovery path remain open. Consolidation identifies `tool.end` versus `agent:post_tool` payload mismatch; verify bridge fix, actual evaluator path, restart state and abuse cases before claiming improvement. Current tool-event outcome gap is separately owned for follow-up. |
| Images in chat | A real PNG/JPEG upload is validated, authorized to the correct session, translated to the provider's image block and reaches a multimodal model; documents remain documents. Spoofed MIME, malformed data and cross-session access are refused and audited. | ArcUI attachment path `packages/arcui/web/src/hooks/use-chat.ts`; gateway media translation under `packages/arcgateway/src/arcgateway/`; relevant tests in `packages/arcgateway/tests/` and `packages/arcagent/tests/`. | A local regression fix is recorded in the program; a real upload-to-vision-provider smoke and negative media/session cases remain required. |
| Tasks and groups | Customer can create, assign, filter, complete and resume tasks/groups after idle, restart and transient failure. Lists are scalable; dependency/subtask/filter projections are computed by the server for paged rows. Role controls and errors are truthful. | ArcUI `packages/arcui/web/src/pages/tasks.tsx`, `components/task-board.tsx`; server paging/facets/projections in `packages/arcui/src/arcui/routes/team_pages.py` and `observe.py`; tests `packages/arcui/web/src/pages/tasks.test.tsx`, `components/task-board.test.tsx`, `packages/arcagent/tests/unit/modules/tasks/test_reliability.py`. | Task paging, global facets and server projections are integrated; scoped tests and prior PostgreSQL evidence are recorded in the execution ledger. Group creation/restart and full customer lifecycle after idle/outage still need acceptance evidence. |
| Workflows, pulses and schedules | Customer creates/runs workflows and schedules in ArcUI. Claims, leases, retries, misfire policy and per-item failures survive restart/outage; one poisoned run cannot stall peers; progress and terminal failure are visible. | ArcTeam workflow engine and ArcAgent scheduler; UI `packages/arcui/web/src/pages/workflows.tsx`, `pages/workflow-detail.tsx`; tests `packages/arcagent/tests/integration/test_scheduler_integration.py`, `test_workflow_trigger_wiring.py`, `packages/arcteam/tests/unit/workflow/`. | Workflow restart/failure-budget fix `b122761b` has 374 workflow tests and a root fresh rerun; it adds persistent fenced counters and healthy-run isolation. Durable schedule/pulse ownership and customer journey acceptance remain open. |
| Slack and email | Inbound acceptance and outbound delivery are durable and correlated; bounded retry, credential renewal, duplicate/replay control and uncertain side-effect state prevent silent loss or duplicate external actions. | Gateway adapters under `packages/arcgateway/src/arcgateway/adapters/`; messaging and connector tests under `packages/arcgateway/tests/` and `packages/arcagent/tests/`. | Execution ledger status is pending. Require real channel contract/journey tests, outage/expiry/reconnect and crash-boundary reconciliation. |
| HTML reports | Authorized report reads use an isolated sandbox/CSP viewer, bounded untrusted content and provenance; listing metadata excludes private bodies. Scripts, network, origin and privileged-browser access are denied. Missing policy or viewer setup never serves raw HTML. | Backend `packages/arcui/src/arcui/routes/agent_detail/report_preview.py`; UI `packages/arcui/web/src/components/file-tree.tsx`; tests `packages/arcui/tests/test_report_preview.py`, `packages/arcui/web/src/components/file-tree.test.tsx`, and `packages/arcui/web/src/lib/api.test.ts`. | Sanitized static preview, no-script/no-network CSP, sandboxed iframe, cancellation and error behavior are implemented and have focused tests. Acceptance remains open for deployment-scoped read policy and real hosted authority composition; local route/component tests do not prove hosted customer authorization. |
| Help on every screen and field | Every shipped route and conditional control explains purpose, setup, current status, safe action and common failure. Every dynamic Settings field gets schema/path-accurate help or an explicit unavailable/generic extension description. Links, route IDs and field-path matching are checked. | `packages/arcui/web/src/content/screen-help.json`, `components/help.tsx`, `lib/help.ts`, `lib/help.test.ts`, `components/help.test.tsx`; route source `packages/arcui/web/src/app/router.tsx`; inventory [arcui-field-help-inventory.md](arcui-field-help-inventory.md). | Commit `8b7135ee` added hosted components and setup help (19 routes, 677 fields, 529 Settings fields before queue help); commit `8b03638b` adds queue help, page/router/nav, API/CLI and built assets, bringing the catalog to 20 routes and 684 fields (529 Settings fields). The seven queue help IDs are `queue.jobs.state`, `queue.control.paused`, `queue.limits.max_concurrent`, `queue.limits.max_queued`, `queue.limits.wait_timeout`, `queue.limits.history_limit`, and `queue.cancel.status`. Root reports 161 web queue/setup/help tests passing; catalog coverage does not establish product acceptance. Setup has five attached help IDs: `setup.status`, `setup.password`, `setup.password_confirmation`, `setup.create_account`, and `setup.sign_in`. Skill revision UI remains subject to its production signer/anchor gates. |
| Connections and Knowledge setup | A granted connection card shows per-agent auth/enrollment, sync/index revision, pending work/error and remediation. Customer can configure, choose resources, approve mapping, sync/reindex/revoke and retrieve searchable knowledge. Credentials are scoped, masked, renewable and never exposed in samples/logs. | ArcUI Connections/Knowledge pages; `packages/arcagent/src/arcagent/modules/connected_data/`; tests `packages/arcui/web/src/pages/connections.test.tsx`, `packages/arcagent/tests/integration/test_connection_grants.py`, `packages/arcagent/tests/modules/connected_data/`. | Existing generic connection/config controls do not themselves satisfy source enrollment lifecycle. Test from a real granted account through searchable knowledge and revocation; missing provider or authority must be explicit. |
| $20 hosted setup, account authority and provisioning | Payment -> one tenant/deployment -> real scoped authority -> first claim/setup -> capability readiness -> usable ArcUI, with BYOK and declared quotas/costs. Duplicate/lost payment or provider responses reconcile idempotently; account authority is nonexportable and survives restart. `awaiting_setup` is distinct from infrastructure readiness. On cancellation, customer data is retained for 30 days under the approved retention policy, then deleted by the documented lifecycle; export/recovery is available during retention. | Browser setup page: `packages/arcui/web/src/pages/setup.tsx`; Arc Cloud remains separate for broker/provisioner work. Arc-side seams include `packages/arctrust/src/arctrust/`, ArcUI server/auth and ArcCLI setup; cloud refs in integration plan (`arc-cloud/provisioner/src/arccloud/`, `site/src/pages/ready.astro`). | Hosted rekey primitives are committed at `0a06bcef` (9 files; root reports 16 tests), following canonical signed-grant primitives `4fa78fce` (9 tests). Arc Cloud setup slice `b34b928` is on separate Arc Cloud `main`; `POST /api/orders/{id}/setup` checks email session, live payment and provider machine, then seals the outbox before delivery. Reported checks: 67 passed excluding `tests/test_machine_broker_proof.py`, full Ruff clean, strict mypy over 13 sources clean, diff check clean. The DGX scan found no configured Vault, only names; the user approved a new authority service; it is not provisioned or live-accepted. Excluded broker/restart WIP, accepted-run integration, real machine factory/restart and configured Vault remain open. No push/deployment or new production validation is claimed; no launch readiness. The user confirmed a 30-day cancellation recovery window and source-state retention policy. Actual provider export, deletion execution and backup behavior remain unverified. Preserve $20/month BYOK behavior. |
| Hosted lifecycle: updates, rollback, restore, isolation | Signed immutable tested artifacts update atomically; failed activation rolls back runtime without changing durable state or restoring revoked authority. Secrets never appear in process args/userdata/logs. Backup restore and tenant isolation are proven. | Runtime installer/update and deployment code; deployment acceptance commands and runbook evidence recorded in execution ledger. | Runtime/update/restore proof remains open. Peer-reported DGX/Azure deployment is `0e516029` only. Newer local source has not been pushed or deployed; no soak is claimed. |
| Incident attribution and sustained operation | Sanitized incident timeline ties sessions, structured error classes, dependency transitions and recovery to an exact deployed artifact without exposing prompts/credentials. Crash/partition/expiry/idle tests and staged soak establish recovery, error/cost budgets and customer-visible status. | Structured telemetry, ArcUI Activity/Audit, deployment/operator runbooks; `docs/runbooks/operate/business-reliability.md`; integration-plan production evidence rules. | Root cause of reported outage is not established by the initial NATS/process observations or broad log grep. Full suite, deployment sync/load soak and sustained proof are absent. Earlier diagnostic token exposure needs authenticated rotation explicitly verified. |

## Detailed criteria for open capabilities

The following criteria preserve the detailed open requirements for skill lifecycle, report viewing, skill-improvement outcomes, and memory/shared-knowledge promotion. They supplement the summary rows above; none of these capabilities is accepted by local helper tests alone.

### Acceptance rules

- A capability is complete only when its customer-facing production path and
  its failure behavior meet the criteria below. A unit test of an isolated
  helper does not close a capability.
- An explicit typed `unavailable` result means the capability is safely absent
  and remains **open**. It is not success, degraded completion, or evidence
  that the feature works.
- Every implementation must preserve the package seams and optional-removal
  behavior in `AGENTS.md`. Missing signers, anchors, trusted sources, or fleet
  attachments must fail closed with the declared typed result, not a memory
  fallback or import/startup failure.
- Record the exact command and revision, dependencies and configuration,
  test-data class, exercised customer path, observed state and audit evidence,
  and any remaining deployment limits in the execution ledger. Keep local
  code/test evidence separate from deployment and soak evidence.

### Skill import, revision, and activation

**Current status:** Open. The consolidation handoff records production
signer/anchor factories, authenticated startup composition, and customer
recovery as acceptance gates. The integration plan says the ArcTrust monotonic
anchor contract and ArcUI factory injection point exist, while anchored
revision resolution/editing and factory-to-runtime composition remain
incomplete.

### Required behavior

1. A real ArcAgent customer startup receives its skill signer, revision
   resolver, and monotonic anchor factory before capability loading begins.
2. Import and edit use the customer surface to produce a complete immutable
   signed bundle. Activation binds the agent and authorized scope to a new
   anchored version and records the previous and target digests.
3. The runtime consumes the exact bytes whose complete bundle signature and
   anchor version were checked. Reload re-verifies both content and current
   anchor; a workspace edit cannot silently replace the active skill.
4. Rollback is an authorized, newly signed activation of a previously verified
   bundle. It advances the anchor and audits actor, scope, new version,
   previous digest, target digest, and outcome. It never rewinds or deletes the
   anchor.
5. Missing authority is a visible typed unavailable state. It must not enable
   unsigned activation or silently load a mutable workspace copy.

### Evidence required to close

- An end-to-end test through the actual agent factory and customer import/edit
  surfaces, followed by activation, runtime use, reload, and authorized
  rollback. Route-only and resolver-only tests do not suffice.
- Tests for modified bundle bytes, partial/missing bundle files, stale or
  conflicting anchor digest/version, unauthorized edit/activation/rollback,
  anchor outage, signer outage, and crashes around anchor advance and state
  persistence. Refusals must be audited and must leave the prior verified
  activation intact or return typed unavailable.
- Evidence that the running skill uses the verified content-addressed bytes,
  including after restart, plus audit records for activation and rollback.
- In each deployment composition claimed as supported, verify configured
  signer/anchor factories and customer recovery. Mock providers alone do not
  establish deployment acceptance.

### Isolated HTML report viewer

**Current status:** Open. The acceptance register records deployment-scoped read policy and hosted authority composition as open, with component/security review evidence still required.
The consolidation handoff includes production authority and deployment-scoped
read policy among the remaining skill/report gates.

### Required behavior

1. A report is viewable only after an authenticated caller passes the
   deployment-scoped read policy for that report. Knowing a URL, report ID, or
   filesystem path grants no access.
2. Rendering is isolated with the approved sandbox and Content Security Policy.
   Report content is bounded, treated as untrusted, and cannot escape into the
   ArcUI origin or reach privileged browser capabilities.
3. The report view preserves provenance sufficient to identify its source,
   producing run/revision, and integrity. Access is authorized and audited;
   private report bodies are not exposed in listing metadata.
4. Missing read authority, malformed or oversized content, and viewer setup
   failure produce a clear unavailable/error state without serving unsafe raw
   content.

### Evidence required to close

- Authenticated customer-path tests for allowed and denied report reads across
  agents/tenants/deployments, including guessed IDs and direct asset requests.
- Browser/component security tests for script execution, event handlers,
  `javascript:` URLs, external resource loading, frame escape, origin access,
  CSP enforcement, sandbox restrictions, and bounded input/output. Verify the
  rendered page, not only response headers or string sanitization helpers.
- Tests for missing/changed source provenance, content limit boundaries,
  concurrent replacement, and absent policy authority. Denials and successful
  reads must have the expected audit events without leaking report bodies.
- A completed independent component/security review and the exact frontend
  lint, type, build, and relevant test results recorded with the revision.

### Skill-improvement outcome bridge

**Current status:** Open. Same-call run/session correlation and event outcome mapping remain in progress. Outcome capture and promotion must not be claimed until the production bridge and route-to-runtime path are verified.

### Required behavior

1. The production run event bridge and skill hook share one typed payload
   contract for successful and failed tool outcomes. The contract carries the
   fields needed to identify the agent, run/session, skill/tool, result, and
   outcome without exposing secrets or private bodies unnecessarily.
2. A successful eligible outcome reaches the actual improvement evaluator.
   Errors, cancellation, timeout, malformed payloads, or ineligible skills
   cannot be interpreted as success or trigger promotion.
3. The customer surface reports truthful, bounded states for eligible,
   blocked, evaluating, and promoted outcomes, with a reason for blocked
   states. State changes correspond to persisted/audited events.
4. Evaluation enforces configured spend and change bounds. Promotion remains
   subject to the required authorization, signature, review, and activation
   path; an observed outcome alone never authorizes executable skill changes.
5. If the event bridge, evaluator, or required authority is absent, report a
   typed unavailable/blocked state and do not imply outcome capture or
   promotion.

### Evidence required to close

- Contract tests proving the emitted production event is accepted unchanged by
  the registered listener for success and explicit error outcomes, including
  schema validation and correlation fields.
- An end-to-end test from a real tool completion through run bridge, listener,
  evaluator, visible status, and authorized promotion/activation. Include a
  negative case proving no promotion occurs from failure, cancellation,
  timeout, replay, malformed data, or an unapproved change.
- Tests for duplicate and out-of-order events, stale run/skill revisions,
  tenant/agent mismatch, spend/change ceilings, evaluator outage, and audit
  failure. Verify bounded idempotent handling and audit of refusals.
- Customer UI/API evidence that each state and reason is truthful and remains
  available after reload/restart; include the typed unavailable behavior.

### Memory promotion and shared knowledge

**Current status:** Open with explicit unavailable behavior. The consolidation
handoff states that automatic promotion raises
`MemoryPromotionUnavailableError` because verified score grants and trusted
source/fleet attachment are unavailable; nightly enabled-promotion follows the
same unavailable path. Shared-knowledge entity promotion raises
`SharedKnowledgeUnavailableError` pending signed contributor/provenance
design. Ordinary signed-document promotion is available and is a distinct
capability; it does not prove either open path complete.

### Automatic memory promotion

#### Required behavior

1. Promotion requires a verifiable score grant and trusted source identity,
   with tenant/agent scope, provenance, freshness, and authorization checked at
   the public seam.
2. Source revocation has a stable fence: a revoked or stale source cannot race
   with promotion, survive restart as trusted, or re-enter through a cache or
   nightly job.
3. ArcTeam attaches and detaches the fleet extension through the public
   lifecycle seam on already-started agents. ArcMemory remains team-agnostic;
   absence of ArcTeam or collection mechanics is a typed unavailable result.
4. Nightly consolidation uses the same trust, authorization, revocation, audit,
   and idempotency rules as interactive promotion. Enabling the schedule cannot
   convert unavailable trust into a successful no-op or fabricated completion.
5. Until these dependencies are composed, both interactive and nightly paths
   remain explicitly unavailable and expose that state to the caller.

#### Evidence required to close

- Contract tests for default, fake, and each real score/source implementation,
  plus architecture/startup tests with the optional ArcTeam or memory
  collection component physically absent.
- End-to-end grant-to-promotion and nightly promotion tests using real trusted
  source/fleet attachment composition, with signed provenance and persisted
  searchable result verified through the public retrieval path.
- Abuse/fault tests for forged or wrong-scope score grants, source substitution,
  revocation during promotion, stale revisions, replay, cache/index poisoning,
  attach/detach failure, process restart, and audit/anchor failure. Verify
  refused operations are audited and do not leave promoted residue.
- Tests showing missing trust, attachment, or collection capability returns
  the documented typed unavailable result without import failure, memory
  fallback, or a success-shaped response.

### Shared-knowledge entity promotion

#### Required behavior

1. Cross-agent or multi-contributor entity promotion has an approved signed
   contributor and provenance contract. Each contribution remains attributed,
   scoped, and independently revocable; canonicalization cannot launder
   classifications or erase provenance.
2. Authorization verifies contributor identity, tenant/agent scope, purpose,
   freshness, signature, and revocation before accepting or retrieving the
   promoted entity.
3. Until the contract and implementation exist, this path returns
   `SharedKnowledgeUnavailableError`. Ordinary signed-document promotion is
   not evidence that entity promotion is available.

#### Evidence required to close

- A documented, reviewed signed contributor/provenance contract and public
  typed seam, with no ArcMemory-to-ArcTeam dependency or internal-table access.
- End-to-end multi-contributor promotion and retrieval tests, including
  contributor/source revocation and removal, and proof that returned knowledge
  preserves contributor provenance and policy scope.
- Refusal/audit tests for forged signatures, replay, stale revisions,
  cross-tenant contributors, revoked sources, classification laundering,
  poisoned indexes, and unavailable optional fleet/collection components.

### Status recording

For each subsection, mark acceptance complete only after all listed behavior
and evidence are recorded against an exact source revision. Record each
deployment composition separately. Keep `unavailable` as the status while a
required signer, anchor, policy, trusted source, or fleet capability is absent;
do not count it as a passing feature. Do not infer production acceptance from
local tests, a healthy process, or ordinary signed-document promotion.

The source status references are the [consolidation handoff](business-reliability-consolidation.md),
[execution ledger](business-reliability-execution.md), and
[integration plan](business-reliability-integration-plan.md).

## Help and customer-documentation ownership

The live help catalog and field-level screen inventory are owned in `packages/arcui/web/src/content/screen-help.json` and `docs/design/arcui-field-help-inventory.md`. The operator-facing [ArcUI screen guide](../runbooks/operate/arcui-screen-guide.md), [Connections guide](../runbooks/operate/connections.md), and Google account setup [guide](../runbooks/operate/google-accounts.md) are integrated in the current source. Do not count a guide or help entry for an absent control as implemented functionality. Each newly integrated control needs its own exact field/action guidance and a route/field coverage check.

Queue contextual help, page, router, navigation, API and CLI are committed at `8b03638b`; runtime controls remain under verification, with no product-acceptance claim. The skill revision UI is present; its dedicated help IDs await the feature owner's canonical control contract. The safe static report preview exists and is documented as `report.preview.open`. Existing inventory notes on conditional controls, Settings path resolution, secret handling and extension fallbacks are normative for coverage.

## Status

Current committed help evidence is `c701b2cc` (190 frontend tests, 143 focused help tests); workflow restart/failure-budget evidence is `b122761b` (374 workflow tests, root fresh rerun). These scoped results do not close original product requirements. All acceptance rows remain open absent their exact customer-path, failure/abuse, and composition evidence. Local worktrees/branches except main were removed after clean ancestor checks. Arc Cloud remains separate; no newer local source push/deployment or soak is claimed.
