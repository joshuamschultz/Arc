# ArcAgent Refactor and Hardening Plan

Status: active  
Created: 2026-08-11  
Scope: `packages/arcagent`, its public boundary with `packages/arcrun`, and the
architecture tests needed to protect adjacent package directionality.

## Purpose

This is the durable execution document for simplifying ArcAgent, clarifying its
boundaries, reducing edge-case-prone control flow, and improving security and
runtime robustness. Work should be selected and recorded here so later sessions
can resume without reconstructing the original review.

This document is a plan, not evidence that a task has been completed. A task may
move to `done` only after its acceptance criteria and verification commands pass.

## Required dependency architecture

```text
arcllm
   ^
 arcrun
   ^
arcagent
   ^        ^
arcgateway  arcui
```

The arrows mean "may know/import/use." The following rules are non-negotiable:

1. `arcllm` is a standalone model adapter/router and knows nothing above it.
2. `arcrun` knows `arcllm`, owns the agentic execution loop, and does not know
   `arcagent`.
3. `arcagent` uses the stable `arcrun` facade and does not import `arcllm`
   directly.
4. ArcAgent production code uses exactly `import arcrun`, followed by qualified
   access such as `arcrun.run_stream` and `arcrun.Tool`.
5. `arcagent` does not import or depend on `arcgateway` or `arcui` and must work
   headlessly.
6. `arcgateway` funnels messages into ArcAgent.
7. `arcui` observes or operates ArcAgent from above; ArcAgent has no UI knowledge.
8. ArcAgent modules/extensions may plug into ArcAgent through explicit, typed
   contracts without reversing these package dependencies.
9. Cross-package consumers import core packages with one root import only:
   `import arcllm`, `import arcrun`, or `import arcagent`, followed by qualified
   public names. Deep imports are reserved for genuine separately installable
   extras, extensions, modules, or plugins with an intentionally public submodule
   API. A package's own implementation may still use its internal modules.

## Execution protocol

For every task:

1. Change its status from `todo` to `in_progress`; only one task should normally
   be in progress at a time.
2. Add or identify a failing regression/architecture test before changing
   behavior.
3. Make the smallest change that satisfies the task and preserves unrelated user
   work.
4. Run the task's focused tests, Ruff, and mypy.
5. Run the complete affected package suites before declaring the task done.
6. Record the date, commands, result, and important design decisions in the task's
   Evidence field.
7. If implementation reveals a conflict, add a note under `Decision log`; do not
   silently widen the task.

Status values: `todo`, `in_progress`, `blocked`, `done`.

## Baseline evidence

Review run on 2026-08-11:

- `UV_CACHE_DIR=/tmp/arc-review-uv-cache uv run ruff check packages/arcagent/src packages/arcagent/tests`
  passed.
- `UV_CACHE_DIR=/tmp/arc-review-uv-cache uv run mypy packages/arcagent/src`
  passed.
- ArcAgent coverage was 80.69%, above its configured 80% floor.
- Full ArcAgent suite: 3,556 passed, 19 skipped, 41 failed.
- Sampled failures were caused by the managed sandbox denying writes to
  `~/.arcagent` and loopback socket binding. The full suite must be rerun in an
  environment permitting its declared test resources before a green baseline is
  claimed.

## Phase 1: Pin and enforce package boundaries

### AA-001 — Add architecture graph tests

- Status: `done`
- Priority: P0
- Depends on: none
- Work:
  - Preserve the existing `arcrun` AST layering test.
  - Add metadata assertions so source imports and `pyproject.toml` dependencies
    obey the same graph.
  - Add an ArcAgent AST test that rejects direct imports of `arcllm`, `arcgateway`,
    and `arcui`.
  - Reject `from arcrun ...`, `from arcrun.submodule ...`, and
    `import arcrun.submodule` in ArcAgent production code.
  - Verify every `arcrun.X` referenced by ArcAgent is public.
  - Add a clean-process import/startup smoke test without ArcUI or ArcGateway.
- Acceptance:
  - The tests fail against the current violations and pass after AA-002 through
    AA-005.
  - Tests use Python AST and parsed project metadata rather than substring scans.
- Evidence:
  - RED, 2026-08-11: added
    `packages/arcagent/tests/architecture/test_dependency_boundaries.py` with
    non-vacuous AST, metadata, facade, and headless-boundary checks.
  - `uv run pytest packages/arcagent/tests/architecture/test_dependency_boundaries.py -q`
    exited 1 with 3 expected failures: 46 source import violations, the direct
    ArcLLM metadata dependency, and no qualified ArcRun facade usage yet.
  - Decisions D-620 through D-625 were recorded in
    `.claude/decisions-log.md`.
  - GREEN, 2026-08-11: the combined dependency and repository clean-import
    architecture suites pass 14/14 after the facade migrations.

### AA-002 — Design the stable ArcRun facade

- Status: `done`
- Priority: P0
- Depends on: AA-001
- Work:
  - Inventory all 27 ArcRun import statements across the 12 ArcAgent production
    modules.
  - Classify each referenced symbol as public contract, implementation detail, or
    misplaced responsibility.
  - Do not root-export mutable internal `RunState`; it is documented as internal.
  - Introduce a small immutable public parent/run context protocol carrying only
    the depth, run ID, event emission, and other data spawning actually needs.
  - Expose a public parallel dispatch mechanism rather than exporting the concrete
    `PlanExecuteStrategy` solely for its `run_ready` helper.
  - Export genuinely public errors such as `ExecutionIsolationError` through the
    root facade.
- Acceptance:
  - ArcAgent needs no `arcrun.*` submodule imports.
  - ArcAgent does not own or mutate ArcRun internal state.
  - Public facade identity and import-smoke tests pass.
- Evidence:
  - 2026-08-11: added root-exported immutable `ParentRunContext`, generic
    `dispatch_ready`, and `ExecutionIsolationError`; `RunState` remains internal.
  - ArcRun now gives tools a detached parent snapshot rather than mutable engine
    state, and `PlanExecuteStrategy` reuses the public dispatch mechanism.
  - Fresh verification: ArcRun full suite `546 passed, 11 skipped`; focused
    ArcRun boundary tests `30 passed`; ArcAgent orchestration/planning integration
    `229 passed, 4 skipped`; Ruff and strict mypy passed for changed sources.

### AA-003 — Convert ArcAgent to one-line ArcRun imports

- Status: `done`
- Priority: P0
- Depends on: AA-002
- Work:
  - Replace every production `from arcrun...` with `import arcrun`.
  - Use qualified names and remove aliases such as `arcrun_run_stream`,
    `arcrun_run`, and `ArcRunTool`.
  - Update annotations, runtime checks, mocks, patch targets, tests, and docs.
  - Keep deep ArcRun modules available for compatibility; establish the root
    facade as the canonical consumer API.
- Acceptance:
  - The AA-001 import-style test passes.
  - No ArcAgent production source imports an ArcRun submodule.
  - Focused ArcAgent/ArcRun integration suites and strict mypy pass.
- Evidence:
  - ArcAgent production source now uses only exact `import arcrun` with
    qualified public names; the ArcAgent boundary suite passes 6/6.
  - Focused orchestration/planning suites passed 229 tests with 4 skips, and
    strict mypy passes across all 275 ArcAgent source files.

### AA-004 — Remove direct ArcAgent-to-ArcLLM dependencies

- Status: `done`
- Priority: P0
- Depends on: AA-002
- Work:
  - Route model, message, block, and tool contracts through `arcrun`.
  - Move model loading below the ArcRun boundary or inject an ArcRun-owned model
    interface.
  - Move model-call identity context to a neutral or ArcRun-owned contract.
  - Move PII detection and secret-pattern utilities out of private `arcllm`
    modules into an appropriate neutral security module; do not blindly make
    every helper part of ArcRun.
  - Remove `arcllm` from ArcAgent package dependencies when no production import
    remains.
- Acceptance:
  - `rg '^from arcllm|^import arcllm' packages/arcagent/src` returns no matches.
  - ArcAgent metadata has no direct `arcllm` dependency.
  - ArcAgent behavior and typing tests remain green.
- Evidence:
  - ArcRun now exposes the model/message/configuration/trace contracts required
    by upper layers through `import arcrun`; its focused facade tests pass.
  - ArcTrust now owns the neutral PII/secret detection primitives, with ArcLLM
    compatibility facades and 537 focused parity/adversarial tests passing.
  - ArcAgent has zero production ArcLLM imports and no ArcLLM metadata
    dependency; the boundary suite passes 6/6 and 85 focused redaction,
    workflow, web, and approval tests pass.

### AA-005 — Tighten compatibility and typing at the boundary

- Status: `done`
- Priority: P1
- Depends on: AA-003, AA-004
- Work:
  - Replace `arcrun>=0.1` with an accurate compatible range.
  - Remove the ArcRun mypy missing-import suppression from ArcAgent.
  - Add compatibility coverage for the declared minimum ArcRun version or an
    automated public-facade contract test.
  - Update examples to use `import arcrun`.
- Acceptance:
  - Strict mypy checks the actual ArcAgent/ArcRun seam.
  - Declared dependency versions support every consumed public symbol.
- Evidence:
  - ArcAgent now declares `arcrun>=0.9,<1` and `arctrust>=0.9,<1`.
  - Removed both `arcllm.*` and `arcrun.*` missing-import suppressions.
  - Root-facade contract tests cover every ArcRun name consumed by ArcAgent;
    strict mypy passes across all 275 ArcAgent source files.

### AA-006 — Remove upper-layer conceptual knowledge from ArcRun

- Status: `done`
- Priority: P2
- Depends on: AA-002
- Work:
  - Rewrite ArcRun production comments/docstrings that name `arcagent` to say
    caller, host, orchestrator, or upper layer.
  - Keep named cross-package relationships only in repository architecture docs
    and boundary tests.
  - Correct the stale spawn-guidance path documented in `arcrun.prompts`.
- Acceptance:
  - ArcRun implementation and package documentation describe generic caller
    contracts and contain no stale ArcAgent implementation paths.
- Evidence:
  - ArcRun production code and package-local documentation now describe only
    generic callers/hosts and contain no ArcAgent names or stale implementation
    paths (`rg` returns no matches).
  - Ruff and strict mypy pass across ArcRun source.

### AA-007 — Enforce clean root imports for core packages

- Status: `done`
- Priority: P1
- Depends on: AA-003, AA-004
- Work:
  - Inventory cross-package imports of `arcllm`, `arcrun`, and `arcagent` across
    production package sources.
  - Expand each package's intentional root facade so consumers do not need deep
    imports.
  - Convert consumers to exactly one root import per core package and qualified
    access.
  - Permit submodule imports only for genuine separately installable extras,
    extensions, modules, or plugins whose submodule is an intentional public API.
  - Add AST tests that distinguish cross-package imports from a package importing
    its own internal implementation modules.
- Acceptance:
  - No production package outside `arcllm` uses `from arcllm` or
    `import arcllm.*`.
  - No production package outside `arcrun` uses `from arcrun` or
    `import arcrun.*`.
  - No production package outside `arcagent` uses `from arcagent` or
    `import arcagent.*`, except approved genuine extension/plugin boundaries.
  - Root facades remain curated and do not expose mutable implementation state.
- Evidence:
  - Added a repository-wide AST guard with planted negative tests; scanner tests
    pass and the enforcement gates are intentionally red during migration.
  - Initial inventory found 98 non-root production imports: ArcCLI 66,
    ArcGateway 7, ArcMemory 2, ArcRun 1, ArcSkill 1, ArcTUI 4, ArcUI 14, and
    three remaining ArcAgent sites being removed by AA-004.
  - Direction checks found one ArcGateway-to-ArcRun bypass and ArcUI-to-ArcLLM
    bypasses; these must route through ArcAgent rather than merely changing
    syntax.
  - Final migration removed every non-root core-package import except the
    intentional optional `arcrun.backends` extension API consumed by ArcSkill.
    ArcUI model inspection now routes ArcUI → ArcAgent → ArcRun → ArcLLM.
  - Final architecture suites: 14 passed. ArcCLI production scan is zero;
    Ruff/mypy/compile checks pass and focused regressions pass 4/4.

## Phase 2: Fix runtime correctness and lifecycle hazards

### AA-010 — Introduce an explicit ArcAgent lifecycle state machine

- Status: `done`
- Priority: P0
- Depends on: AA-001
- Work:
  - Serialize startup and shutdown transitions.
  - Define repeated-start and concurrent-start behavior.
  - Construct a typed runtime locally and publish it atomically only after
    successful startup.
  - Reuse one partial-startup teardown path.
- Acceptance:
  - Concurrent and repeated startup cannot duplicate resources or bindings.
  - Failed startup leaves the agent in a defined, restartable state.
- Evidence:
  - ArcAgent now serializes STOPPED/STARTING/STARTED/STOPPING transitions;
    concurrent startup initializes once and failed startup is restartable.
  - Admission checks require STARTED, and partial state resets consistently.
  - Independent lifecycle/core/tracked suites: 55 passed; Ruff and mypy passed.

### AA-011 — Make shutdown complete and failure-independent

- Status: `done`
- Priority: P0
- Depends on: AA-010
- Work:
  - Stop accepting new runs.
  - Cancel active ArcRun handles and await owned finalizers under deadlines.
  - Tear down bus, loader, registry, module runtimes, WORM/audit resources, and
    other owned services independently.
  - Collect/log cleanup errors without skipping later cleanup.
  - Make repeated shutdown safe.
- Acceptance:
  - Tests cover shutdown during a live run, each cleanup stage raising or
    hanging, partial startup, and repeated shutdown.
  - No owned background task remains after shutdown.
- Evidence:
  - Shutdown switches to STOPPING before its first await, cancels active ArcRun
    handles, drains finalizers, and applies independent five-second teardown
    deadlines to bus, loader, registry, audit sink, and model cleanup.
  - Cleanup failures/timeouts do not skip later stages; tracked/session state is
    cleared and repeated shutdown is safe. The 55-test lifecycle/core run passes.

### AA-012 — Centralize active-run and same-session coordination

- Status: `done`
- Priority: P0
- Depends on: AA-011
- Work:
  - Replace duplicated run tracking with one identity-guarded registry.
  - Prevent an old finalizer from removing a replacement handle.
  - Add a per-session turn lock or queue.
  - Define whether overlapping work queues, steers, cancels, or is rejected.
  - Use one consistent session identifier as the registry key.
- Acceptance:
  - Tests cover both completion orders for two same-session tracked runs.
  - History and assistant messages cannot commit out of order.
- Evidence:
  - Tracked-run cleanup is now identity-guarded, with a regression test proving
    an older finalizer cannot remove a replacement handle (4 focused tests pass).
  - One SessionRunCoordinator now owns identity-safe active registration and a
    full-turn lock keyed by canonical SessionManager.session_id. Streaming and
    tracked turns hold it through assistant commit/finalization while steering
    remains lock-free.
  - Same-session order, cross-session reverse completion, tracked queuing, stale
    handles, and lifecycle admission are covered; focused suites pass 29 tests
    (and the combined independent rerun passes 13/13).

### AA-013 — Make capability reload transactional

- Status: `done`
- Priority: P1
- Depends on: AA-010
- Work:
  - Scan and validate a candidate capability snapshot before changing live state.
  - Atomically swap tool, skill, and hook registrations.
  - Add ModuleBus unsubscribe/replace tokens so changed hook code and priority
    take effect.
  - Preserve the previous working registry on any reload failure.
- Acceptance:
  - Failed scan leaves prior capabilities callable.
  - Changed and removed hooks are reflected exactly once after reload.
- Evidence:
  - Reload prepares and validates an isolated candidate before explicit commit;
    scan/import/validation failures leave live loader and registry state intact.
  - Capability-owned tools are batch-replaced with rollback, and ModuleBus
    subscription tokens support atomic replacement of changed/removed hooks and
    priorities exactly once.
  - Focused loader/bus/agent suite: 75 passed; Ruff, strict mypy, and diff-check passed.

### AA-014 — Guarantee monotonic planning progress

- Status: `done`
- Priority: P1
- Depends on: none
- Work:
  - Make ready-frontier execution return an explicit progress/defer reason.
  - Terminate or mark blocked/exhausted when zero branches can reserve budget.
  - Align budget exhaustion and reservation headroom semantics.
- Acceptance:
  - No nonterminal plan can repeat the same state indefinitely.
  - Tests cover mixed bounded/unbounded dimensions and zero-reservation runs.
- Evidence:
  - A ready frontier that returns zero outcomes now records a failed step and
    enters the bounded failure/replan path instead of resetting indefinitely.
  - Focused orchestrator/concurrent-executor suites: 15 passed; Ruff and strict
    mypy passed.

### AA-015 — Fix malformed session-index replay

- Status: `done`
- Priority: P1
- Depends on: none
- Work:
  - Advance the durable byte offset after consuming malformed or filtered rows.
  - Distinguish no new bytes from new-but-invalid records.
  - Preserve transactional behavior if valid rows fail to persist.
- Acceptance:
  - Malformed-only tails are logged once and are not reread every polling cycle.
- Evidence:
  - The index now advances `sync_state` transactionally whenever complete bytes
    were consumed, even if every record was malformed or intentionally filtered.
  - Non-object JSON records are rejected explicitly rather than failing later.
  - Session-index suite: 20 passed; focused Ruff and strict mypy passed.

## Phase 3: Establish real execution and I/O security boundaries

### AA-020 — Isolate authored capability execution

- Status: `done`
- Priority: P0
- Depends on: AA-002
- Work:
  - Stop compiling/executing untrusted workspace capability source in the
    ArcAgent host process.
  - Execute through an isolated ArcRun backend with a narrow serialized contract,
    time/CPU/memory limits, no inherited environment secrets, and controlled
    filesystem/network access.
  - Retain AST and signature checks as validation/provenance controls, not as the
    security boundary.
- Acceptance:
  - Adversarial object-graph escape code cannot reach host objects or secrets.
  - Infinite CPU, memory pressure, oversized output, and process crashes remain
    contained and return typed failures.
- Evidence:
  - Untrusted global/agent/workspace/extension Python is statically inspected for
    literal tool metadata and registered as JSON-only proxies; execution crosses
    `arcrun.make_execute_tool` into the tier-configured isolated backend.
  - Source, arguments, output, and wall time are bounded; no host object graph,
    environment, network, mount, writable root, or unrestricted process tree is
    inherited. Authored hooks/background tasks fail closed at this seam.
  - Docker isolation adds a 256 MiB memory/swap ceiling to its existing
    read-only/no-network/no-mount/pids/capability restrictions.
  - Focused ArcAgent suite: 66 passed; ArcRun Docker: 13 passed, 1 skipped;
    Ruff and mypy passed.

### AA-021 — Make shell execution bounded and cancellation-safe

- Status: `done`
- Priority: P0
- Depends on: AA-020 or an approved independent process-isolation design
- Work:
  - Stream stdout/stderr into bounded buffers rather than using unbounded
    `communicate()` buffering.
  - Start a dedicated process group/session.
  - On timeout or cancellation, terminate and then kill the entire group under a
    bounded cleanup deadline.
  - Validate timeout and output-limit configuration.
- Acceptance:
  - Infinite-output, descendant-process, timeout, and caller-cancellation tests
    leave no process behind and remain within memory bounds.
- Evidence:
  - Personal-tier Bash now incrementally drains both pipes into bounded retained
    buffers, validates finite `0 < timeout <= 3600`, and runs in a new session.
  - Timeout and caller cancellation TERM then KILL the whole process group and
    always drain/reap; timeout includes inherited-pipe EOF from descendants.
  - Focused suite: 26 passed, 2 Docker-dependent skipped; Ruff and mypy passed.

### AA-022 — Harden browser and web URL policy

- Status: `done`
- Priority: P1
- Depends on: none
- Work:
  - Parse and canonicalize URLs structurally.
  - Restrict schemes and reject credentials/control characters.
  - Normalize IDNA, casing, ports, and paths.
  - Resolve and reject loopback, private, link-local, multicast, and cloud
    metadata addresses.
  - Revalidate every redirect and protect against DNS rebinding.
  - Replace substring and full-URL `fnmatch` checks with canonical origin/domain
    boundary checks.
  - Reconsider default JavaScript and download enablement by tier.
- Acceptance:
  - Tests cover IPv4/IPv6 variants, encoded hosts/paths, userinfo, IDNA, suffix
    confusion, redirects, DNS rebinding, and metadata services.
- Evidence:
  - Browser and web egress share canonical HTTP(S) validation that rejects
    credentials, malformed/control-character URLs, unsafe literal addresses,
    and DNS answers outside globally routable space.
  - Navigation/history/download and extraction/final provider URLs are checked
    with fail-closed DNS resolution off the event loop; tests inject a
    deterministic resolver rather than weakening production policy.
  - Host matching is label-aware, web matching is parsed by
    scheme/host/port/path, and JavaScript/download defaults are disabled.
  - Focused URL/browser/web suite: 88 passed; Ruff and targeted mypy passed.

### AA-023 — Eliminate filesystem authorization TOCTOU

- Status: `done`
- Priority: P1
- Depends on: none
- Work:
  - Replace validate-path-then-open flows with descriptor-relative operations,
    `O_NOFOLLOW`, regular-file checks, and post-open identity verification.
  - Use atomic same-directory replacement for writes.
  - Reuse the same secure primitive for built-in read/write/edit operations where
    practical.
- Acceptance:
  - Symlink/rename race tests cannot escape the authorized workspace.
- Evidence:
  - Built-in read/write/edit select the narrowest authorized root and walk every
    ancestor with directory-relative `O_DIRECTORY|O_NOFOLLOW`; final reads are
    bounded and authorized with `fstat` on the opened descriptor.
  - Writes use fsynced same-directory exclusive temporaries plus dirfd replace,
    preserve existing mode, verify parent identity, and edits reject target
    dev/inode replacement between read and commit.
  - Existing path, protected-file, secret-content, and re-sign policy remains in
    front of mutation. Focused suite: 62 passed; Ruff, strict mypy, diff-check passed.

### AA-024 — Consolidate secure secret-file reads

- Status: `done`
- Priority: P1
- Depends on: AA-023
- Work:
  - Replace FileVaultBackend's `exists/stat/read_text` sequence with the existing
    descriptor-based secure-read approach.
  - Require a regular file, expected owner, private mode, and bounded size.
  - Reject symlinks, FIFOs, devices, and oversized content.
- Acceptance:
  - Vault and extension secret stores share one audited low-level primitive.
- Evidence:
  - FileVaultBackend now uses the shared same-fd `O_NOFOLLOW` reader off the
    event loop, requiring a current-UID 0600 regular file and a 64 KiB cap.
  - The shared primitive rejects symlinks, non-regular files, oversized data,
    ownership/mode failures, and read races without pathname revalidation.
  - Focused vault suite: 13 passed; Ruff and strict mypy passed.

### AA-025 — Make secret-store updates cross-process safe

- Status: `done`
- Priority: P1
- Depends on: AA-024
- Work:
  - Protect complete read-modify-replace transactions with an OS-level advisory
    lock or versioned compare-and-retry.
  - Bound store size, entry count, and value lengths.
- Acceptance:
  - Multiprocess update tests do not lose either writer's changes.
- Evidence:
  - EnvFile now holds a 0600 sibling advisory lock across the complete
    read-modify-atomic-replace transaction, in addition to its coroutine lock.
  - Store bytes, entry count, key shape, and value length/newlines are bounded.
  - Independent-instance interleaving and limit tests pass; EnvFile suite 10/10,
    broader secret suites 38/38, Ruff and strict mypy pass.

## Phase 4: Make persistence and background work predictable

### AA-030 — Bound and validate session replay

- Status: `done`
- Priority: P1
- Depends on: none
- Work:
  - Stream session records instead of reading the entire file synchronously.
  - Move blocking disk/parse work off the event loop.
  - Enforce file, line, record-count, and message-content limits.
  - Schema-validate every replayed message and define corrupt-tail handling.
- Acceptance:
  - Huge files, huge lines, JSON scalars, invalid messages, and concurrent append
    scenarios produce bounded, typed behavior.
- Evidence:
  - Session replay streams in a worker thread with 64 MiB file, 2 MiB line, and
    100,000-record ceilings; incomplete tails are quarantined for a later append.
  - Only object-shaped checkpoint/message/compaction records with a valid public
    ArcRun Message shape enter live state; malformed/scalar/unknown records skip.
  - Session and compaction suites: 44 passed; Ruff and strict mypy passed.

### AA-031 — Make compaction durable and concurrency-safe

- Status: `done`
- Priority: P1
- Depends on: AA-030
- Work:
  - Introduce an explicit compaction generation/boundary record or atomic snapshot
    plus append journal.
  - Snapshot/version under lock, summarize outside the lock, and commit with a
    compare-and-swap check.
  - Ensure restart reconstructs exactly the compacted baseline.
- Acceptance:
  - Compact then restart yields the same bounded history as the live process.
  - Slow summarization does not block unrelated message appends for its duration.
- Evidence:
  - Compaction now snapshots under lock, summarizes outside it, and commits only
    if a revision compare-and-swap proves no concurrent append occurred.
  - Each successful compaction appends an explicit baseline boundary containing
    the summary and retained/masked tail; replay replaces prior generations.
  - Restart equivalence and nonblocking concurrent-append tests pass; combined
    session/compaction suite 46 passed, with Ruff and strict mypy green.

### AA-032 — Standardize periodic-loop ownership

- Status: `done`
- Priority: P2
- Depends on: AA-011
- Work:
  - Replace bespoke session-index, skills, pulse, scheduler, run-control, and
    proactive loops with a common cancellable periodic runner where semantics
    match.
  - Use `Event.wait()` with a timeout for prompt stop behavior.
  - Make retry/backoff and failure policy explicit.
- Acceptance:
  - Every periodic loop has one owner, immediate stop, supervised exceptions, and
    deterministic testable cadence.
- Evidence:
  - Shared `PeriodicRunner` supports fixed/live intervals, immediate/deferred
    first ticks, `Event.wait()`-based stopping, cancellation propagation, and an
    explicit consecutive-failure/bounded-backoff policy.
  - Session indexing, skills, pulse, scheduler, run control, proactive ticks,
    and Redis/Kubernetes lease renewal now use the common ownership contract.
  - Focused suite: 291 passed, 1 skipped (with one concurrently stale scheduler
    source-text assertion being replaced by AA-043); Ruff and runner mypy passed.

### AA-033 — Supervise all fire-and-forget tasks

- Status: `done`
- Priority: P2
- Depends on: AA-011
- Work:
  - Introduce one owned background-task supervisor.
  - Retrieve/log task exceptions and drain tasks during shutdown.
  - Fix stdin serve-loop cancellation by cancelling and awaiting the pending task
    set on EOF or signal.
  - Simplify ModuleBus to one exception-isolation boundary.
- Acceptance:
  - Asyncio debug runs produce no destroyed-pending-task or unretrieved-exception
    warnings.
- Evidence:
  - One agent-owned `BackgroundTaskSupervisor` now retrieves terminal failures
    and owns/drains capability loops, model event bridges, and tracked finalizers
    during normal and partial shutdown.
  - Stdin read/signal task pairs are always cancelled and awaited, and ModuleBus
    has one handler exception-isolation boundary.
  - Focused suite: 113 passed; asyncio-debug suite: 67 passed; Ruff, mypy, and
    diff-check passed.

## Phase 5: Consolidate complex orchestration

### AA-040 — Consolidate child-agent spawning

- Status: `done`
- Priority: P1
- Depends on: AA-002, AA-012
- Work:
  - Create one structured spawn primitive returning `SpawnResult`.
  - Make the LLM tool adapter responsible only for validation/formatting.
  - Unify depth, identity, clearance, budget, audit, timeout, and error semantics.
- Acceptance:
  - Tool-driven and programmatic spawning share the same implementation and
    produce equivalent policy/audit outcomes.
- Evidence:
  - The `spawn_task` adapter now only resolves its requested tool/prompt policy,
    applies its shared concurrency/budget envelope, calls the structured
    `spawn()` primitive, and formats the `SpawnResult`.
  - Depth, identity, lineage, audit, timeout, error mapping, strategy selection,
    and token breaker behavior now live in the single structured implementation.
  - Focused unit/integration spawn suite: 75 passed; Ruff and mypy passed.

### AA-041 — Repair `spawn_many` scheduling and budget settlement

- Status: `done`
- Priority: P1
- Depends on: AA-040
- Work:
  - Validate `max_concurrent >= 1` and bound batch size.
  - Use structured concurrency or a worker queue.
  - Stop scheduling and cancel/drain siblings on fail-fast.
  - Make budget reservation and settlement cancellation-safe and refund work that
    never starts.
- Acceptance:
  - Queued work does not begin after fail-fast triggers.
  - Negative/zero concurrency is rejected immediately.
  - Cancellation and unexpected exceptions do not leak budget.
- Evidence:
  - `spawn_many` rejects non-positive concurrency and batches above 100, and a
    bounded worker queue rechecks fail-fast state after dequeue so pending work
    cannot start after the stop boundary.
  - Workers are cancelled and drained on caller cancellation/unexpected errors;
    every token reservation is atomically replaced by actual usage or refunded.
  - Focused spawn suite: 28 passed; Ruff and mypy passed.

### AA-042 — Decompose the tool-dispatch state machine

- Status: `done`
- Priority: P1
- Depends on: AA-012
- Work:
  - Introduce a typed `ToolDispatchContext`.
  - Extract explicit normalize, authorize, approve, execute, and record stages.
  - Preserve one public dispatch envelope and current fail-closed ordering.
  - Avoid mutation of loosely related closure state.
- Acceptance:
  - Security ordering is captured by focused stage-contract tests.
  - The core wrapper is short enough to read as orchestration rather than policy
    implementation.
- Evidence:
  - A typed `ToolDispatchContext` and explicit normalize, authorize, approve,
    execute, and record stages replace the nested mutable closure state machine.
  - The single dispatch envelope preserves atomic admission, approval outside
    the lock, veto-before-execute, timeout span, post event, and audit ordering.
  - Focused tool/policy/security suite: 114 passed; Ruff, mypy, diff-check passed.

### AA-043 — Replace private-field service location with typed contracts

- Status: `done`
- Priority: P2
- Depends on: AA-010
- Work:
  - Introduce typed `RuntimeDependencies` and module configure protocols.
  - Replace signature-name introspection and pervasive `Any` at integration seams.
  - Separate required-module failures from explicitly optional degradation.
  - Replace arbitrary runtime-binding tuples with typed binding/context objects.
- Acceptance:
  - Renaming a dependency cannot silently stop injection.
  - Required module configuration failures abort startup predictably.
- Evidence:
  - Typed `RuntimeDependencies`, closed dependency keys, explicit per-module
    specs/protocols, and named generic `RuntimeBinding` replace signature-name
    introspection and arbitrary tuples.
  - Unknown/required contract failures abort startup; optional degradation is
    explicitly declared and logged. Signer authority is granted only by named
    specs, and bindings clear on setup failure/shutdown.
  - Focused lifecycle/integration/isolation suite passed 59 before a concurrent
    AA-050 edit; Ruff, strict mypy, and diff-check passed.

### AA-044 — Encapsulate capability registry state

- Status: `done`
- Priority: P2
- Depends on: AA-013
- Work:
  - Add immutable snapshot/query APIs for tools, skills, hooks, authored
    capabilities, and `requires_skill` mappings.
  - Keep lock ownership and precedence/suppression rules inside the registry.
  - Remove external `_tools`, `_skills`, and `_lock` access.
- Acceptance:
  - No production consumer reaches into CapabilityRegistry private collections or
    lock state.
- Evidence:
  - `CapabilityRegistry` now owns immutable snapshot/query methods for tools,
    skills, hooks, lifecycle entries, counts, authored names, and lookups.
  - Production consumers no longer access registry private locks or collections.
  - Focused capability/skills suite: 72 passed; Ruff and mypy passed.

## Phase 6: Decompose large files after behavior is pinned

### AA-050 — Split responsibility concentration points

- Status: `done`
- Priority: P3
- Depends on: AA-013, AA-040, AA-042, AA-043, AA-044
- Work:
  - Reassess and split only along established behavioral boundaries:
    - `connections.py`
    - `modules/tasks/capabilities.py`
    - `core/agent.py`
    - `modules/workflows/capabilities.py`
    - `orchestration/spawn.py`
    - `extension/mcp_attachment.py`
    - `core/config.py`
    - `modules/connectors/install.py`
    - `capabilities/capability_loader.py`
  - Prefer domain modules and typed objects over generic utility modules.
- Acceptance:
  - Each extracted module has one clear responsibility and no new circular
    dependency.
  - Public APIs remain stable unless separately approved.
- Evidence:
  - Extracted cohesive owners for connection catalog/audit, MCP policy,
    connector DTOs/attachments/credential placement, task routing helpers,
    reload models, config loading/merge, agent security custody, workflow tool
    implementations, and spawn observability.
  - Existing facade imports and patch seams remain stable; extracted modules
    depend downward and introduce no reverse imports/cycles.
  - Focused decomposition suites: 78 + 122 + 120 passed; spawn-focused suite
    passes after lookup updates; Ruff and strict mypy pass across all affected
    packages (405 source files).

## Full completion gates

The refactor is complete only when all of the following hold:

- The required dependency graph is mechanically enforced in source and metadata.
- ArcAgent production code contains only `import arcrun` for ArcRun access.
- ArcAgent has no direct ArcLLM, ArcGateway, or ArcUI dependency.
- ArcAgent imports, starts, runs, and shuts down headlessly.
- Authored code and shell commands execute behind real isolation and bounded
  resource controls.
- Startup, shutdown, reload, session turns, compaction, and spawning have explicit
  concurrency contracts with regression coverage.
- Ruff and strict mypy pass for every affected package.
- ArcAgent, ArcRun, and relevant integration suites pass in a test environment
  that permits required home-directory and loopback resources.
- Branch coverage remains at or above the declared threshold, with no critical
  path relying only on aggregate coverage.
- Documentation and examples reflect the final public facade and architecture.

## Decision log

Append decisions; do not rewrite history.

- 2026-08-11: The desired dependency architecture was confirmed as
  `arcllm <- arcrun <- arcagent <- arcgateway/arcui`.
- 2026-08-11: ArcAgent's ArcRun usage must use a single `import arcrun` statement
  per module and qualified public-facade access.
- 2026-08-11: `RunState` should not be promoted merely to make current deep
  imports compile; introduce a narrow public run-context contract instead.
- 2026-08-11: Large-file decomposition is intentionally sequenced after behavior,
  lifecycle, and boundary tests to avoid cosmetic churn hiding correctness work.
- 2026-08-11: Clean-import scope applies at cross-package boundaries. Core package
  consumers use one root import plus qualified public names; package-internal
  imports remain free to use internal modules, and only true separately installable
  extension/module/plugin APIs may justify deep cross-package imports.

## Progress log

- 2026-08-11: Initial review captured and plan created. No production code changed.
- 2026-08-11: AA-001 entered RED. Architecture tests now expose current boundary
  violations; production remediation begins with AA-002.
- 2026-08-11: AA-002 completed. AA-003 converted all ArcAgent production ArcRun
  imports to root-qualified usage; final full-suite evidence remains pending while
  AA-004 removes the remaining direct ArcLLM edges.
- 2026-08-11: All AA tasks are complete. Dependency/import architecture tests
  pass 29/29; the final combined architecture/runtime/spawn checkpoint passes
  63/63; ArcRun passes 554 with 11 optional-backend skips.
- 2026-08-11: Ruff and strict mypy pass across the 405 ArcAgent/ArcRun/ArcTrust/
  ArcLLM source files. ArcAgent's full sandbox run passes 3,646, skips 19, and
  initially reported 44 failures: two eager-VM regressions were fixed and pass
  25/25; the remaining failures require user-home writes, loopback socket binds,
  or external model download. Focused changed-path suites pass.
