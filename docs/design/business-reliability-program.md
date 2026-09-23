# Business reliability and self-service deployment

Date: 2026-09-23. Status: investigation and first repairs in progress; not a release certification.

## Product outcome

A customer pays $20/month on a website, clicks Deploy, and configures and tunes a working Arc fleet in ArcUI. Routine provisioning, dependency outages, reconnects, restarts, updates and recovery require no intervention from the product owner. Permanent failures must give the customer an actionable explanation and preserve accepted work. An installation returning HTTP 200 is not sufficient evidence of a working product.

The primary incident reference is the installation reached through `ssh dgx`. Chats were working during investigation but failed the previous night. All source changes belong in the local Arc repository on `main`; DGX investigation is read-only. Deployments must use a tested repository artifact, never server-only patches.

The model split for this work is Astra orchestration/design, Sol research/implementation, and optional Luna screen documentation after behavior is verified. Existing specifications and mechanisms should be repaired and connected, not replaced wholesale.

## Evidence rules

- **Observed:** collected from the running DGX installation; sanitized metadata only.
- **Static defect:** a concrete source path demonstrates the failure mechanism, but does not prove it caused the reported incident.
- **Hypothesis:** requires a reproduction, profiling, or incident correlation.
- **Fixed locally:** regression fails before the change and passes afterward. This does not mean deployed or proven over weeks.

Do not infer that memory data was lost from an error message alone. Distinguish failed writes, lost records, stale indexes, authorization denial, empty retrieval, and UI visibility. Do not infer queue durability from a semaphore or successful replies from an inbox acknowledgement.

## Initial deployment evidence

The first bounded DGX inspection found Arc active since September 21 with zero systemd restarts, a live NATS child listening on loopback, and `/api/health` returning 200. Disk and available memory were not exhausted at that moment. The installed runtime path ended in `0.5.0-130403ed`; the local investigation baseline was `main` at `1850fd3a`. Exact build ancestry and artifact contents need verification before deciding which local findings affect DGX.

The NATS child also started on September 21. Its observed process lifetime does not support broker death/restart as the cause of the September 22 incident. Client/consumer recovery and timeout correlation remain open investigations.

A bounded sample of last-night logs contains many timeout and NATS-related matches. Counts from broad regular expressions are not incident counts or root-cause evidence. The investigation must isolate structured error classes, timestamps and correlation IDs without publishing prompts, credentials or customer content.

Service diagnostics exposed access tokens present in command arguments. Do not reproduce those values. Remove credential-bearing command arguments and rotate the affected tokens through the authenticated operator surface; rotation is outstanding until explicitly verified.

## Repair order and ownership

| ID | Priority | Work | Owner and boundary | Completion evidence |
|---|---|---|---|---|
| BR-01 | P0 | Reproduce DGX outage; identify deployed artifact and dependency failures | Deployment/operator layer | Sanitized incident timeline joining affected sessions, dependency errors and recovery; no conjecture presented as cause |
| BR-02 | P0 | Supervise managed NATS and workflow hosts; truthful component readiness | Deployment lifecycle; gateway host; ArcTeam workflow engine remains unchanged | Kill child/dependency in an isolated deployment; parent survives or exits according to policy; service recovers without manual restart; readiness reflects unavailable capabilities |
| BR-03 | P0 | Restore fleet attachment after broker unavailable during startup | ArcAgent optional messaging attachment through ArcTeam public seam | Start with NATS unavailable, restore it, exchange signed direct and group messages without restarting agent; no silent permanent in-memory substitution |
| BR-04 | P0 | Durable inbox work and reply delivery | ArcTeam inbox ownership; ArcAgent run acceptance; ArcGateway channel delivery | Crash at each boundary; every accepted message reaches visible terminal status; successful tools are not rerun merely because reply delivery failed |
| BR-05 | P0 | Streaming calls honor ArcLLM queue admission | ArcLLM queue module | Streaming and nonstreaming calls share limits; cancellation, close, error and timeout release capacity; no buffering of the whole stream |
| BR-06 | P0 | Uploaded images reach multimodal model input | Canonical gateway media validation; ArcUI upload; agent translator | Actual PNG/JPEG upload becomes an image block; document stays document; spoofed MIME and cross-session access remain denied |
| BR-07 | P0 | Connector, memory and knowledge durable recovery | Connector/source lifecycle; ArcMemory mechanics; ArcStore persistence | Granted source -> selection -> approved mapping -> sync -> searchable agent knowledge; reconnect/restart/reindex/revoke and failed writes verified |
| BR-08 | P0 | Durable, controllable model-call queue | ArcLLM public queue seam, optional durable implementation; adjacent facades for consumers | Persisted identities and states, fair admission, bounded wait/service time, recovery, UI and CLI controls, security tests |
| BR-09 | P1 | Bounded chats and complete prompt/trace correlation | ArcAgent context/session; ArcRun loop/deadlines; ArcLLM provider calls | One trace joins session, run, effective prompt versions, strategy, queue call and reply; hanging provider/tool yields progress and terminal timeout/cancel |
| BR-10 | P1 | Reliable tasks, groups, pulses, workflows, Slack and email | Respective module contracts and durable orchestration owners | Customer creates and executes through real UI; scheduled work survives restart and outage; misfires and uncertain side effects have explicit policies |
| BR-11 | P1 | Fast, fresh ArcUI screens | ArcUI API and query/render layer | Bounded requests and pagination; idle-tab revalidation; measured screen budgets; dependency-specific failures do not block unrelated screens |
| BR-12 | P1 | Skill import/edit and bounded automatic improvement | ArcSkill optional capability and authenticated artifact lifecycle | Import -> verify -> approve -> activate; edit -> re-sign/version -> reload; real outcomes trigger evaluation with budget and promotion guardrails |
| BR-13 | P1 | Self-service provisioning and lifecycle | Website/control plane outside agent nucleus | Duplicate payment/provision requests create one tenant/instance; full install health journey; all settings available through ArcUI; automated update/recovery |
| BR-14 | P2 | Safe HTML report viewer | Optional ArcUI artifact presentation seam | Agent report opens in sandboxed viewer; scripts, credential access and network exfiltration refused by default; authorization, size limits and audit verified |
| BR-15 | P1 | Screen help and customer recovery instructions | ArcUI help content, derived from actual screens/contracts | Every supported screen explains purpose, setup, status, safe actions and common failures; links and field references checked in CI |
| BR-16 | P1 | Reliable tools and optional capabilities | ArcAgent registry and typed tool/extension contracts | Real tool discovery, authorization, execution, timeout, cancellation and teardown tested for every shipped transport; unavailable optional tools degrade explicitly without breaking unrelated agents |

P0 means a prerequisite to a dependable paid service, not a claim that every entry is a confirmed active outage. The first bounded local patches target BR-05 and BR-06. The remaining items require implementation and validation; this document does not mark them complete.

## Durable execution and delivery design

Transport acknowledgement, acceptance of work, model completion and successful reply delivery are distinct states. Persist the correlation between the inbound event, authorized run and outbound delivery. Acknowledge inbound transport after durable work acceptance, not after a fire-and-forget wake and not after keeping a broker acknowledgement open for an entire LLM turn.

Use fenced claims, bounded retry schedules and durable terminal outcomes. A restarted worker reconciles accepted work and pending replies. Retrying a reply must not rerun successful model/tool actions. For external side effects without an idempotency/reconciliation contract, record an uncertain outcome and require the appropriate customer action rather than blindly replaying it. Do not promise exactly-once behavior across arbitrary external APIs.

Preserve existing ArcFlow durable state and leases. Supervise the process that drives them. A dead runner must make workflow capability unavailable and visible; one poisoned run must not stop other runs. Expired authorization must be re-evaluated during recovery. Signed request freshness and authorized recovery of an already accepted operation are separate concerns.

## ArcLLM queue design

The current per-model, in-memory queue is useful local admission control, but is insufficient for the requested hosted product. SPEC-014's original scope does not provide durable customer controls.

The contract belongs to ArcLLM. ArcRun consumes its public facade; ArcAgent consumes the ArcRun facade. ArcUI and ArcCLI obtain authorized control through public adjacent seams. A durable backend is replaceable and optional for standalone library use; ArcLLM must not import ArcTeam or higher-layer agent services. Hosted configuration must explicitly require the durable capability rather than silently reverting to an in-memory queue.

Persist a call ID, tenant/agent/session/run correlation, provider capacity scope, priority, timestamps, deadlines, attempt count, idempotency metadata and state. Separate `queued`, `claimed`, `running`, `succeeded`, `failed`, `cancel_requested`, `cancelled` and `outcome_unknown`. Encrypt any persisted payload and bind access to identity, tenant, classification and purpose. Redact queue summaries; payload viewing is separate authorization.

Apply bounded queue capacity and both queue-wait and provider-service deadlines, subordinate to the enclosing run deadline. Enforce provider concurrency/rate/token budgets across calls sharing the actual capacity pool. Use fair scheduling so chat traffic cannot starve maintenance and maintenance cannot consume all interactive capacity. Separate policy configuration from provider-specific implementations.

ArcUI and ArcCLI must expose the same contract: list/filter calls, view age and position estimate, inspect reason for waiting, cancel, pause/resume admission, and change authorized concurrency/priority/budget settings. A cancellation response must distinguish requested from confirmed; cancellation cannot guarantee reversal of an already dispatched provider request or bill.

On restart, safe queued work can be reclaimed. In-flight requests with unknown provider outcomes require provider-supported reconciliation/idempotency or an explicit policy and visible uncertainty. Do not silently charge for duplicate requests. Queue mutations require signed authorization, audit, stale-revision checks and idempotency.

## Knowledge and connection reliability

Track connection authorization health separately from source sync and index health. The connection card must show each granted agent's enrollment, latest successful durable sync, indexed revision, pending work, errors and remediation. A working tool is not proof of searchable knowledge.

Commit source progress only with durable record/index scheduling. Resume incremental ingestion from a committed checkpoint. Preserve raw provenance and classification, reconcile vector/text indexes, and make rebuilds idempotent. Revoke access immediately at retrieval authorization, including stale caches and pending jobs. A source outage must not erase previously committed knowledge or silently switch the agent to empty memory.

## Prompt, session and tool correctness

Test both interactive streaming and background/inbox execution. Allocate correlation before prompt construction, then carry it through session, prompt snapshot, strategy, queue, model trace, tools and delivery. Verify the actual provider-bound request includes the effective system context and selected strategy prompt, with artifact hashes/versions. A visible prompt template is not proof that the running request used it. Required prompt verification failures must stop execution explicitly.

Distinguish missing trace data from failed joins, unavailable storage and permission denial. Persist sensitive trace content only through the encrypted, authorized storage seam; operator payload inspection and export require their own audited permissions. Expose safe IDs and status even when payload access is denied.

Each shipped tool needs a real production-path contract test, including disconnected transports, expired credentials, malformed results, cancellation and cleanup. Generated/imported skills must exercise the same tool authorization path. Removing an optional capability must leave startup and unrelated tools functional. Recovery must revalidate signed artifacts and scoped authority rather than reusing stale trust.

## Hosted service and $20 economics

Start by making a single hosted tenant reliable; do not add multi-replica agent dispatch merely because an unimplemented distributed executor exists. Maintain explicit tenant isolation and a removable fleet composition layer. Provisioning control-plane code must not move into ArcAgent.

Use an idempotent lifecycle: payment verified -> tenant reserved -> provisioning -> health validation -> ready, with visible failed/retryable states and compensating cleanup. Verify webhook signatures, reject replay, reconcile billing and provisioning periodically, and make duplicate/out-of-order events harmless. Define cancellation, retention and deletion separately; payment failure must not silently destroy customer data.

A $20 plan needs measured hosting/support costs, per-tenant CPU/RAM/storage/agent limits and model spend ceilings. Whether model usage is customer-funded or included remains a product decision; no unlimited model allowance is assumed. Cost ceilings must include memory consolidation, embeddings, pulses, workflows and skill evaluation, not just chats.

Publish immutable signed build identities, perform post-update customer-journey checks, and automatically roll back a failed runtime activation without touching durable state. Rollback may not restore revoked authority or weaker security policy. Test backup restoration and key-custody recovery in an isolated environment. Credentials must never become process arguments, browser-visible reusable secrets, log payloads or ordinary files.

## Proposed release gates

These are initial engineering targets, not observed performance or contractual SLAs. Measure under a declared workload, tenant count and hardware profile before promising them commercially.

| Gate | Initial target |
|---|---|
| Critical screen usable | p95 <= 2 seconds warm; large histories use bounded pagination |
| Command accepted or explicitly rejected | p95 <= 1 second excluding external provider execution |
| Waiting/running visibility | Progress or explicit waiting state within 2 seconds; no indefinite spinner |
| Routine local dependency recovery | <= 60 seconds after dependency returns, under declared test load |
| Accepted work durability | No silent disappearance across tested process crashes; terminal or actionable pending/uncertain state |
| Isolation | One failing agent, connector, poison message or workflow cannot stop unrelated work |
| Release rehearsal | Full paid-deploy/setup/chat/knowledge/tasks/workflows journey from a fresh tenant without SSH |
| Sustained operation | 7-day staging soak with injected failures, then 30-day canary observation with explicit incident accounting |
| Restore/update | Verified backup restore and failed-update rollback without loss of committed state or authority downgrade |

Fault cases include broker loss, database restart, provider 429/5xx, stalled streams, revoked/expired credentials, DNS/network partitions, process kill between commits, stale leases, duplicate events, full disk, exhausted pool, malformed rows, browser sleep and reconnect, and updates while work is pending. Run destructive fault injection only in isolated deployments with synthetic data.

Each feature must have a production-path test from the customer's entry point. Retain the package unit/integration suites, strict types, lint, coverage and architecture gates; add real dependency and browser journeys. Existing CI excludes `slow` tests and does not by itself establish recovery of a deployed service. Mark missing required dependencies as a failing release gate, not a silent skip.

Security-sensitive changes must add abuse cases to the existing adversarial runner at `tests/run_adversarial_tests.py` (the root instructions reference a `scripts/` path that is absent in this checkout). Test all tiers, optional-component deletion, cross-tenant/agent denial, tampering, replay, stale authorization and audit failure. Reliability must never be achieved by bypassing these controls.

## Research to retain

The detailed baseline evidence, code locations, source links and repro proposals are retained in [infrastructure research](business-reliability-research/infrastructure.md), [execution research](business-reliability-research/execution.md), and [experience research](business-reliability-research/experience.md). These are research snapshots; subsequent fixes and verification belong in the implementation record below.

- Existing `.claude/plans/self-serve-deploy-plan.md`, especially fleet reliability and in-app documentation.
- Existing SPEC-014 queue, SPEC-025 channel resilience, SPEC-043 loop controls, SPEC-044/054 skill improvement, SPEC-059 live turns, SPEC-061 ArcFlow, SPEC-064 connection surfaces, SPEC-065 media, SPEC-073 ingestion, SPEC-075 Slack, and SPEC-081 skill packages. Their completion markers do not replace current runtime-path tests.
- [NATS consumer semantics](https://docs.nats.io/nats-concepts/jetstream/consumers): durable consumption and acknowledgement must be paired with application work state.
- [Temporal workflow execution](https://docs.temporal.io/workflow-execution) and [activity execution](https://docs.temporal.io/activity-execution): reference patterns for persisted progress and bounded external work, not a decision to replace ArcFlow.
- [Pi agent loop](https://github.com/badlogic/pi-mono/blob/main/packages/agent/src/agent-loop.ts) and [Rowboat](https://github.com/rowboatlabs/rowboat): comparison material for explicit events and collaborative agent UX; not evidence of Arc incident causes.
- Local Breakdowns: `Agent Harness Engineering - A Survey.md`, `A Practical Guide for Designing, Developing, and Deploying Production-Grade Agentic AI Workflows.md`, `A Practical Approach for Building Production-Grade Conversational Agents with Workflow Graphs.md`, and `From LLM Inference to Agentic Workloads - Characterization and Implications for Serving Systems.md`.

The references `qm` and `grok build` remain unidentified; do not attribute capabilities to them without a verified source.

## Implementation record

All work below is local to `main`. DGX has not been updated or restarted. No paid provisioning, billing or queue-control UI is implemented by these patches.

| Repair | Scope | State |
|---|---|---|
| BR-05 streaming queue | Shared stream/nonstream admission; provider-await deadlines; cancellation and iterator ownership through model wrappers; zero-wait capacity behavior | Fixed locally; full ArcLLM suite, Ruff and strict mypy passed |
| BR-06 image input | Classify stored media from validated detected MIME; one canonical classifier; actual upload/claim/image-block regression; spoofed MIME refusal | Fixed locally; full ArcGateway/ArcUI suites, adversarial battery, Ruff and strict mypy passed |
| BR-07 catalog monitor recovery | Retry unavailable catalog at a bounded interval without scheduling stale registrations; preserve cancellation and durable source state | Fixed locally; relevant tests, wheel architecture, Ruff and package strict mypy passed |

The initial security-battery failure exposed a test isolation defect: the approvals fixture changed `ARC_CONFIG_DIR` but inherited the battery's `ARC_TEAM_ROOT`, causing key creation and route lookup to resolve different test roots. The fixture now isolates both roots. A second stale fixture used an August question timestamp that had expired by the September test run; it now constructs a current question while preserving production expiry semantics.

The catalog-monitor change passed 362 relevant unit/integration/architecture tests (8 skipped, 4 slow tests deselected), followed by all 4 wheel architecture tests with build-dependency network access. Strict mypy checked all 355 ArcAgent source files. This patch adds sanitized diagnostic logging and bounded retry; it does not yet add a monitor-health UI or prove full source/index crash recovery.

Final verification on September 23:

| Check | Result |
|---|---|
| `uv run pytest packages/arcllm/tests -q --tb=short` | 1,471 passed, 1 skipped |
| `uv run --with 'aiohttp>=3.14.3,<4' pytest -q packages/arcgateway/tests packages/arcui/tests` | 2,427 passed, 6 skipped |
| `uv run python tests/run_adversarial_tests.py -q --tb=short` | 413 passed, none skipped; independently rerun after combined changes |
| Combined queue, monitor, media custody, MIME refusal, upload identity and approval regressions | 67 passed, none skipped; independently run together |
| `uv run mypy packages/arcllm/src --strict` | 57 source files clean |
| `uv run mypy packages/arcgateway/src packages/arcui/src --strict` | 156 source files clean |
| `uv run mypy packages/arcagent/src --strict` | 355 source files clean |
| Ruff check and format check on every changed Python file | Passed (23 files) |
| `git diff --check` | Passed |

Commands used `UV_CACHE_DIR=/tmp/arc-uv-cache` where the sandbox disallowed the default cache. The adversarial voice test required loopback socket permission; wheel builds required dependency-download access. Both were rerun with appropriate permissions and passed. Test suites still emit third-party deprecation warnings. This is scoped verification of the changed packages and relevant contracts, not a claim that the entire monorepo or the hosted deployment passed every gate.

Luna drafted the [current ArcUI screen guide](../runbooks/operate/arcui-screen-guide.md). It is standalone documentation; integrating contextual help into the application remains BR-15 work.

The remaining release blockers are still open: incident root cause, broker/runner and scheduled-loop supervision, durable inbox/reply reconciliation, persistent queue controls, complete knowledge recovery tests, customer settings/skill workflows, UI performance, paid provisioning and long-running operational evidence. Rotate the diagnostic-exposed tokens through authenticated operator control before treating DGX credentials as clean.
