# Changelog

All notable changes to ArcAgent (`arc-agent` on PyPI) will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.16.0] - 2026-07-12

SPEC-056 Mission Control: arcagent gets a `tasks` module — the per-agent + team-backlog task
list that turns arc from "agents that message" into "agents that coordinate work," sitting on
top of the SPEC-056/SPEC-032 arcstore mutable plane.

### Added
- **`arcagent.modules.tasks`** — a new module (mirrors the scheduler module's shape:
  `config`/`store`/`_runtime`/`capabilities`) exposing ten tools over the shared arcstore
  `TaskStore`: `create_task`, `update_task`, `start_task`, `complete_task`, `fail_task`,
  `assign_task`, `claim_task`, `list_tasks`, `decompose_task`, `set_task_output`. `list_tasks` is
  `read_only`; the rest are `state_modifying` and audited centrally through the classification
  system. Mutations are owner-only-guarded; free-text fields go through NFKC + injection
  sanitize; `assign_task`/`decompose_task` resolve `@handle` → DID via `arcteam.registry`. The
  store opens the shared arcstore DB (`resolve_data_dir()/store/arcui.db`) so tasks are visible
  cross-agent and aggregate into the team kanban (arcui).
- **Cross-agent assignment notify** — `assign_task` sends one signed `arcteam.MsgType.TASK_ASSIGNED`
  DM to the new owner after the durable arcstore write (best-effort — a notify failure never
  rolls back or masks the write); the messaging inbox handler adopts the task via `start_task`,
  idempotent on redelivery.
- **Mention-scoped inbox activation (SPEC-055, Phase 0B)** — the messaging inbox's
  `_should_activate` gate wakes only the mentioned agent on a channel `@mention` (DMs and
  critical messages still always wake; an un-mentioned broadcast wakes everyone), closing the
  fleet-wide token-cost gap and giving `assign_task`'s notify a real addressed-wake path.
- **Public tool-authoring surface (F6/F7).** `tool`/`hook`/`background_task`/`capability` and the
  new `capability_meta(fn)` accessor are re-exported from `arcagent.tools` — authors no longer import
  the private `arcagent.tools._decorator` or read `_arc_capability_meta` directly.
- **Capability inventory seam** (`arcagent.capabilities.inventory`, T-704/705/711) —
  `collect_capability_inventory` / `collect_agent_capability_inventory` enumerate skills/tools
  across all four scan roots and return the loader's own verdict for each, captured verbatim at
  every terminal decision site via a new `CapabilityOutcome`. Read-only (drives a throwaway
  registry, never mutates the live agent) and posture-faithful (shares `resolve_trust_posture`
  with real agent startup, so a UI verdict equals production at every tier). This is the one
  approved seam arcui's dashboard is allowed to import from arcagent.
- **`ArcAgent.registered_tools`** — public accessor for the live tool registry, backing the
  capability inventory seam above.

### Fixed
- **`@tool`/`@hook`/`@background_task` reject sync functions at decoration time (F8)** — a clear
  `TypeError` at the definition site instead of an opaque failure later in the loader.
- **Unreachable NATS degrades quietly (F9)** — the messaging bootstrap bounds the connect, quiets
  nats-py's async error callback, and degrades to the in-memory bus with a single warning instead of
  dumping a ConnectionRefused traceback on every solo-agent run.
- **Agent-authored and operator-added skills never loaded.** `create_skill` writes to
  `capabilities/skills/<name>`, but only the builtins scan root had a `skills/` subdirectory —
  global/agent/workspace skills silently never loaded (hit live on the reference deployment). All
  three writable roots now get a skills subdir via one shared helper, joining `_UNTRUSTED_ROOTS`
  under the same sign/TOFU gate — nothing newly auto-trusted.
- **`write`/`edit` silently invalidated a signed artifact's signature.** Hand-editing a signed
  capability file broke its `.arcsig` sidecar with no warning; the next load then failed closed.
  New `resign_if_previously_signed` at the runtime choke point refreshes the signature on
  write/edit — an existing sidecar is the signal a file participates in the Sign pillar; ordinary
  workspace files are never newly signed.
- **Personal tier ignored valid self-signatures.** `TofuLayer.evaluate()` denied even artifacts
  the loader had freshly re-verified against the agent's own pinned key unless
  `auto_run_agent_code` was globally enabled — the root cause of a freshly-scaffolded
  `calculator.py` being dead on arrival on every new agent. Signed now allows at personal tier;
  unsigned still denies. `arc agent create` also now signs the scaffolded `calculator.py` itself.
- **Mutation tools silently lied when signing failed.** `sign_artifact_file` no-op'd on missing
  identity and let crypto exceptions crash the tool; all six mutation tools now append an explicit
  "UNSIGNED — will be denied at next load" warning and emit `tool.artifact_unsigned` when signing
  fails — the write itself still succeeds, only the message stops overclaiming.

### Security
- **CRITICAL — cross-agent identity bleed, live-exploited (ASI03).** `builtins.capabilities`
  held per-agent state (workspace, identity **including the private key**, audit sink, tier) as
  plain module globals, on a single-agent-per-process assumption the embedded gateway (up to 32
  concurrent cached `ArcAgent`s in one process) had already broken. Any agent's `startup()`
  silently rebound the globals for every other agent's in-flight tool calls — on the reference
  deployment, one agent's `create_skill` signed with a *different* agent's private key while
  routing and transcripts stayed correct. All nine globals are now `ContextVar`s (zero call-site
  changes); a deterministic forced-interleaving reproducer (`asyncio.Event`) fails pre-fix, passes
  post-fix through the real `sign_artifact_file` path. `TraceRecord.agent_did` (an existing,
  never-populated field) is now threaded end-to-end so traces carry the verified DID.
- **The same vulnerability class, swept.** All 16 remaining module `_runtime.py` files
  (memory/web/pulse/planning/scheduler/browser/voice/telegram/memory_acl/proactive/slack/skills/
  messaging/policy/user_profile/session) now bind their state through a `ContextVar` too. A new
  AST architecture test fails CI on any `global` statement inside a `_runtime.py` file — a
  contextvars module never legitimately needs one, so this is a zero-false-positive guard against
  the pattern recurring. Background tasks are unaffected — `capability_registry` spawns them from
  the agent's own startup task after `configure()` already ran there, so `asyncio.create_task`'s
  automatic context snapshot isolates each agent's background loops permanently.
- **CRITICAL — the ContextVar fix itself broke every second turn.** `SessionRouter` spawns a
  fresh `asyncio.Task` per inbound turn while agents stay cached; a `ContextVar` bound in turn 1's
  task is invisible to turn 2's sibling task, so every conversation failed with "runtime not
  configured" starting on message two — invisible to unit tests, which always run `configure()`
  and the tool call in the same task. Fixed with a build/bind split: state is built once at
  startup and collected on the agent (`_runtime_bindings`); every `_runtime.py` gained an
  idempotent 2-line `bind()`; `activate_runtime_bindings(agent)` replays them at the top of
  `dispatch_stream`, `start_tracked_run`, and `resume_stream`. Both invariants — second-turn
  success *and* the original cross-agent isolation — are now pinned together through the real
  `SessionRouter` + `AsyncioExecutor` + cached real agents.
- **Self-modification tools bypassed the workspace boundary.** The six read/write/edit/ls/find/
  grep tools already enforced confinement via `resolve_workspace_path()`, silently — no audit on
  denial. The four self-mod tools (`create_tool`/`update_tool`/`create_skill`/`update_skill`)
  built paths by direct join, bypassing the choke point entirely; `update_tool`/`update_skill`
  also accepted raw traversal names. Live incident: an agent installed a skill and a secrets file
  into a **sibling agent's** workspace via its own self-mod tools. All ten path-taking tools now
  route through the one choke point, which emits `tool.workspace_path.denied` (caller DID, tool,
  offending path) on every denial; self-mod tools are additionally confined to
  `<workspace>/capabilities` and validate names against traversal.
- **Pasted secrets were written verbatim to workspace files.** All six content-writing tools now
  run every payload through a two-layer secret guard (`arcllm` `SECRET_PATTERNS` + a
  keyword-anchored heuristic for prefixless keys) and deny with an audited
  `tool.secret_write.denied` event. New `store_secret` builtin takes **no value parameter by
  construction** — it returns tier-specific operator guidance (env file at personal/enterprise,
  vault at federal) rather than accepting and mishandling the secret itself.

## [0.15.0] - 2026-07-08

SPEC-047 — **extensibility as a first-class product property**. Generalized the two
select-one extension seams (Brain / SPEC-041, SkillAdapter / SPEC-044) into one
`ExtensionPoint` + `select_extension` mechanism, added signed preset-config **blueprints**,
and gave config-relaxable tiers one declared surface.

### Added
- `arcagent.extension`: the `ExtensionPoint` descriptor + one `select_extension` (shared
  choice dispatch + fail-closed refuse-before-import BYO gate + dotted-path importer), the
  four-family registry (`brain`/`skills` select-one, `tools`/`hook-builds` scan-many views
  over the SPEC-021 `CapabilityRegistry`), and pure-read `inspect_extensions`.
- `arcagent.blueprints`: signed, versioned TOML preset-config bootstrap. `resolve_blueprint`
  (verify-before-use, fail-closed above personal), `apply_blueprint` (deep-merge UNDER user
  values, stringency-max tier floor — a blueprint can only RAISE a floor), `dumps_toml`
  (materialize the concrete `arcagent.toml` the runtime flat-reads), `list_blueprints`. Three
  provenance-trusted packaged presets: `personal-assistant`, `enterprise-ops`, `federal-analyst`.
- `arcagent.tiers`: `RELAXABLE_KNOBS` table + `resolve_tier_floor` (the `SecurityConfig`
  federal-floor validators delegate to it — dedup) + `audit_tier_relaxations` (the blueprint-
  apply producer for `tier.relaxation_granted`) + tier stringency ordering.

### Changed
- `brain/select.py` + `skilladapt/select.py` are now thin `ExtensionPoint` instances; the
  duplicated dispatch / BYO gate / dotted-path importer was **deleted** (behavior-preserving —
  public `select_brain` / `select_skill_adapter` signatures unchanged).
- `core/config.py` `SecurityConfig` federal-floor enforcement delegates to
  `arcagent.tiers.resolve_tier_floor` (core NCLOC 3498 → 3463).

### Security
- **Blueprint signature verification is PINNED, not TOFU-only (adversarial-review HIGH-1).**
  `resolve_blueprint` / `list_blueprints` now take an `operator_public_key` and, above personal,
  verify a user preset's `.arcsig` against it. Previously the gate called `verify_file` with no
  pinned key, so any self-consistent signature was accepted — an attacker could self-sign a
  malicious preset with a random keypair. An unsigned, tampered, or wrong-key preset is now
  refused before merge; when the operator key cannot be resolved above personal, resolution
  denies fail-closed (an unpinned floor is no floor).
- **`arc ext inspect`/`verify` signed-status is pinned to the agent DID key (HIGH-1).**
  `inspect_extensions` / `_signed_status` accept a `trusted_public_key` so a wrong-key
  self-signed capability reads "unsigned" instead of a false "signed".
- **Blueprint overlay denylist extended (LOW-3):** `security.operator_key_dir`,
  `operator_vault_path`, `notary_keystore`, and `witness_medium_path` are now stripped from a
  blueprint overlay before merge — a preset can no longer redirect operator-key or federal-witness
  custody (co-locating the witness with the operator key would make rollback detection illusory).

## [0.14.0] - 2026-07-08

SPEC-044 — the optional **`SkillAdapter`** self-improvement seam (mirrors the SPEC-041
`Brain` seam). arcagent manages skills (write/load/run) on its own; installing the
optional `arcskill` package and enabling it supercharges them with adaptation and
improvement. arcagent holds only the thin seam — all improvement logic lives in
`arcskill.improver`.

### Added
- `arcagent.skilladapt`: `SkillAdapter` Protocol + `NullSkillAdapter` (improver-less
  default, silent no-op, zero files) + config-select (`none` / `arcskill` / signed BYO,
  lazy import, BYO signing gate above personal).
- `arcagent.modules.skills`: thin wiring — module-bus hooks forward primitive per-turn
  signals to the adapter; builds the injected agent-DID sidecar signer and the
  operator-key WORM audit sink (audit authority ≠ audited subject).
- `create_skill` now scaffolds a runnable `evals/` golden-task suite so new skills are
  improvable from creation (SPEC-044 REQ-070).

### Removed
- `arcagent/modules/skill_improver/` (relocated to `arcskill.improver`, no-legacy):
  arcagent source NCLOC net **down** ~2,400.

## [0.13.1] - 2026-07-07

arcmemory embedder + distiller wiring (SPEC-041, Phase 10). The `Brain` seam now
lights up arcmemory's arcllm-backed embedder and distiller so semantic recall and
consolidation are live in production.

### Changed
- **`select_brain` wires the arcllm-backed seams.** When `brain="arcmemory"`/`"auto"`
  is selected and arcmemory is importable, `select_brain` builds an
  `arcmemory.ArcLLMEmbedder` (unless `embed_backend="none"`) and, when a
  `distill_provider` is configured, an `arcmemory.ArcLLMDistiller` (fresh provider per
  consolidation via `arcllm.load_model`), and injects both into `ArcMemoryBrain`.
  `embed_backend="none"` / an empty `distill_provider` leaves the respective seam
  unwired — recall degrades to BM25 + graph, consolidation is a no-op, never a crash.
- **`modules.memory.config`** gains `embed_backend`, `embed_model`, `distill_provider`,
  and `distill_model` knobs (threaded through `_runtime.configure` → `select_brain`).
- **`Brain.rebuild_index` is now async** (matching arcmemory's async embedder seam);
  `NullBrain.rebuild_index` updated in lockstep.

### Fixed
- Synced `arcagent.__version__` with the packaged version (was drifting at `0.12.0`).

## [0.13.0] - 2026-07-07

arcmemory integration (SPEC-041, Phases 8/9): the memory-less `Brain` seam, thin wiring, deletion of both old memory backends (no-legacy), and grounded reflection into the existing ACE curator. A `pip install arc-agent` alone runs fully memory-less; adding `arcmemory` (or a BYO Brain) activates capture/recall.

### Added
- **The `Brain` seam (`arcagent/brain/`, outside core).** A structural `Brain` Protocol (primitives only — arcagent imports no memory package) + a no-op `NullBrain` default. `select_brain(setting, ...)` config-selects the impl: `"none"` → NullBrain (memory off, zero files), `"arcmemory"`/`"auto"` → the `arcmemory.ArcMemoryBrain` plug-in (lazy import; missing install degrades to NullBrain, never crashes), or a dotted `module:Class` path for a bring-your-own Brain. This is the SPEC-047 pluggable-brain seam.
- **Query-conditioned assembly (T-080, core seam).** `assemble_system_prompt(..., *, query="")` threads the turn text into the `agent:assemble_prompt` payload so recall is query-conditioned. Signature/payload only — core NCLOC unchanged (3498).
- **Thin `modules/memory` wiring (T-081/082/083) — the only arcagent-side memory code.** `Brain.capture()` on `agent:post_tool` + `agent:post_respond` (zero-LLM); `Brain.retrieve()` on `agent:assemble_prompt` @ priority 50 → `sections["recall"]`, query-conditioned with a once-per-turn cache (spawn double-assembly retrieves once); one de-duplicated `memory_search` tool; a `@background_task` that polls an event-count / idle trigger and calls `Brain.consolidate()`, emitting `memory.consolidated`. Every Brain read/write is routed through the priority-10 `memory_acl` veto first. With `NullBrain` selected the module is a silent no-op (writes nothing).
- **Grounded reflection → existing ACE (`modules/policy/reflection.py`, Phase 9).** `ReflectionGrounding{episode_summary, step_results, failures}` + `reflect_and_curate()` feed the EXISTING `PolicyEngine._reflect`→`_curate` (no second curation algorithm). A `memory.consolidated` policy hook grounds a session-less automated run on the consolidation episode (closes the automated-run gap). Writes only `policy.md`/`policy.pending`, never `identity.md` (ASI01); federal stages `policy.pending` for approval, personal/enterprise auto-apply; every mutation audited (`policy.reflected`/`policy.curated`). Reuses the engine's score-clamp/prune/cap/sanitize.

### Removed
- **No-legacy deletion of both old memory backends.** Deleted `modules/bio_memory/` and the old `modules/memory/` internals (`MarkdownMemoryModule`, `HybridSearch`, `EntityExtractor`, the duplicate `memory_search` tool, the dead `ctx.data["memory_context"]` path, and the dead `embedding_model`/`search_weight_vector`/`context_budget_tokens` config). Their salvageable logic was already absorbed into `arcmemory`. `memory_acl` is retained wholesale.

## [0.12.0] - 2026-07-07

SPEC-043 SOTA loop controls (arcagent half): arcrun executes concurrently; arcagent keeps its own accounting atomic. The guards live next to the state they protect — never in the loop.

### Added
- **Concurrency-safe trifecta ledger (REQ-032, the hard part).** `SessionCapabilityLedger.admission_lock(session_id)` — a per-session `asyncio.Lock`. `tool_registry.wrapped_execute` now holds it across the `snapshot → await pipeline.evaluate → record` critical section so concurrent dispatch cannot interleave the TOCTOU window: two calls whose capability-leg *union* completes a forbidden composition are evaluated in sequence and the second is DENIED (no lost update). The lock covers only the O(1) admission decision — `tool.execute` and the HumanGate approval await run OUTSIDE it (no over-locking, no human timeout under the lock). Proven by an interleaving-forced `asyncio.Barrier` test that FAILS on the unguarded ledger and PASSES guarded.
- **Plan aggregate reserve-then-settle (REQ-053).** `Plan.reserved_tokens`/`reserved_cost` + `available_budget()` (remaining − reservations); `ConcurrentStepExecutor` reserves each branch's cap from the shared budget before launch (under a plan-level lock) and settles actual spend on completion, so `Σ(reservations + spend) ≤ Plan.budget` — N concurrent branches can never overspend. Proven by an interleaving-forced test (guarded: no overspend + over-budget branch deferred; control: the naive read-then-run pattern overspends).
- **Concurrent Plan-Execute (REQ-050..056).** `ConcurrentStepExecutor` (satisfies the SPEC-040 `StepExecutor` seam) dispatches the ready DAG frontier concurrently via arcrun's wired `PlanExecuteStrategy` — one gather path, not a second. `PlanOrchestrator` swaps to concurrent frontier dispatch by injection; the `Plan` model + `ready_steps`/replan are unchanged. A failing branch is captured as a `FAILED` outcome and never crashes siblings.
- **Checkpoint persistence (REQ-005).** `SessionManager.persist_checkpoint()` writes each arcrun `LoopCheckpoint` (scalar metadata only — the transcript is already durable) as one append-only JSONL line under the existing lock, emits a `loop.checkpoint` audit event, and segregates checkpoint records from the model transcript on resume (`latest_checkpoint()`).
- **Proactive HITL wiring (REQ-010b/c, ADR-019).** `tools/approval_policy.py` resolves the tier approval ladder — personal empty, enterprise all plain tools, federal every skill+tool (`RegisteredTool.skill_backed`) — and binds `approval_provider` to SPEC-035 `HumanGate` (operator-signed one-shot grant). arcrun enforces the resolved name-set as a dumb predicate. `build_loop_controls` assembles the loop-control kwargs for the streaming run.
- **Federal circuit-breaker floors (REQ-024).** `SecurityConfig` gains `runaway_max_repeat`/`error_cascade_max`/`loop_max_parallel`; the tier validator pins non-relaxable federal floors and fails closed on a looser/disabled override.
- `Tool.classification` is stamped onto arcrun tools in `to_arcrun_tools()` so the wired parallel dispatcher can classify batches.

SPEC-040 real planner: the planning module is now a Plan-Execute planner, not a to-do notebook. Given a goal it produces a durable, dependency-aware DAG plan, executes each step as one bounded, policy- and budget-gated arcrun run, resumes cleanly after a restart, and replans a failed step — bounded so it can never run away.

### Added
- `arcagent/modules/planning/models.py` — `Plan`/`PlanStep` DAG model (LLMCompiler-style `depends_on` edges) with typed `StepStatus`/`PlanStatus`, cycle/dangling/duplicate validation, and a frontier (`ready_steps`) re-derived from `depends_on` + the succeeded set (no separate cursor, so resume reconstructs progress from the plan file alone).
- `decomposer.py` — goal → DAG via arcllm's portable tool-forced structured output (ReWOO-style single upfront decomposition); a grounding gate rejects ungrounded plans and any step targeting a protected identity path (`identity.md`/`policy.md`, ASI01) before persistence; `replan` preserves the succeeded prefix, feeds the model the real results + failure reason (Reflexion-lite), and bumps `version`.
- `store.py` — durable `plans/<id>.json` via the existing atomic-write helper, integrity-checked on read, with a single audit emission point per transition through the arctrust sink (WormSink where configured) + telemetry mirror.
- `executor.py` — the `StepExecutor` Protocol (the SPEC-043 swap point) + interim `ArcRunStepExecutor` driving one bounded react run per step through `arcrun.run`; a policy DENY, SPEC-038 budget breach, or tool error is captured as a `FAILED` step, never a crash.
- `orchestrator.py` — deterministic DAG walk (one ready step at a time; parallel dispatch deferred to SPEC-043), checkpoint-before-proceed, bounded replan with a structured terminator on `max_replans` exhaustion, and crash-safe resume.
- Planner tools `plan_create` / `plan_status` / `plan_replan` / `plan_abandon` (SPEC-021) and an `agent:assemble_prompt` hook injecting the active plan frontier.

### Removed
- The four to-do CRUD tools (`task_create`/`task_list`/`task_update`/`task_complete`), `tasks.json` handling, the redundant `tools.py`, and the dead `PlanningModule` class (no-legacy, OQ-4).

### Notes
- Zero `arcagent/core` LOC added (core NCLOC unchanged at 3411/3500); the planner is entirely a module and drives, never re-implements, the loop.

## [0.10.0] - 2026-07-07

SPEC-039 quality pass: core back under budget, and per-tier default budget ceilings so token/cost/request limits are ON by default (SPEC-038 OQ-3).

### Added
- Conservative per-tier default budget ceilings (`arcagent/tools/_policy_fill.py`). When an operator leaves a ceiling unset, a tier default now applies: federal is the tightest floor (500k tokens / $10 / 500 requests), enterprise a looser cap (2M tokens / $50 / 2k requests), and personal stays unbounded/relaxable. An explicit operator ceiling always wins. Both the arcrun circuit-breaker (`resolve_run_budget`) and the arctrust `ProviderLayer` (`resolve_provider_limits`) are now default-on above personal — a federal agent is never unbounded by omission.

### Changed
- **Behavior:** federal/enterprise agents with no `[budget]` block are now capped by the tier default rather than running unbounded.
- Relocated tool-definition primitives from `core/tool_transport.py` to `tools/_transport.py` (ToolTransport, RegisteredTool, `native_tool`, arg validation) — tool-domain code moved out of the nucleus so `arcagent/core` is back under the 3500 NCLOC budget (3411, 89 to spare). Behavior-preserving; every name is still re-exported through `core.tool_registry`.

## [0.9.0] - 2026-07-06

SPEC-038 sub-scopes A/C/D wiring: arcagent bridges arcrun budget usage onto the policy pipeline, binds clearance to identity, enforces no-exfil at egress, and tags outbound-comms tools so the SPEC-035 trifecta gate fires.

### Added
- Dispatch now fills `PolicyContext.provider_usage` from the live arcrun `RunState` (`ctx.parent_state`) with a TRUSTED, config-sourced provider label (`llm.model`, never `LLMResponse.model`), lighting up the previously-inert SPEC-034 `ProviderLayer`.
- Dispatch fills `PolicyContext.clearance` (caller clearance from identity + per-tool resource classification from `[tools.policy] classifications`), driving the no-read-up `ClassificationLayer`.
- `[security] clearance` + `classification_enforced` and `[tools.policy] classifications` / `egress_clearances` config.
- `EgressProxy` refuses above-ceiling data (`egress.classification_refused`, `EgressClassificationDenied`) — no-exfil with a single external ceiling (`UNCLASSIFIED`) plus per-origin overrides.
- `messaging_send` / Telegram `notify_user` now declare the `external_comms` leg; `messaging_check_inbox` / `messaging_read_thread` declare `untrusted_input`; `browser_navigate` maps to both legs. The lethal-trifecta gate now has real leg producers.
- `spawn()` propagates clearance monotone-non-increasing (child clamped to the parent's clearance).

## [0.8.0] - 2026-07-06

SPEC-037: the operator key resolves through the arctrust `Signer` seam; config selects custody / algorithm / FIPS.

### Added
- `[security]` config: `signing_algorithm` (`ed25519` default | `ecdsa-p256`), `custody` (`in_process` | `vault_transit`), `require_fips` (federal floor). SPEC-037 REQ-004/007/008/009.
- Startup runs `arctrust.assert_fips_if_required(...)` before any signing key is used — fail-closed at federal (SC-13/IA-7).
- `ArcAgent._operator_signer`: the operator key resolved through the `Signer` seam; every WORM/checkpoint signature goes through it.

### Changed
- Every WORM construction site now passes a `Signer` (raw seed deleted): the policy WORM chain, `model_manager.build_checkpoint_sink` / `ensure_model` (`operator_signer`), the checkpoint witness-head signing, and `skill_improver` audit. The messaging audit chain (`AuditLogger`) takes an asymmetric `Signer` built from the agent identity (`_bootstrap.audit_signer`); `MessagingConfig.audit_hmac_key` removed.

## [0.7.0] - 2026-07-06

SPEC-035: lock goals, break the lethal trifecta, and confine bash. Three confinement floors wired at every tier (ADR-019).

### Added
- **Goal-lock (REQ-001..004).** `is_protected_path` / `enforce_protected_path` / `resolve_protected_paths` in `tools/_validation.py`. `write`, `edit`, and `bash` consult one shared guard before any mutation; the default protected set (`identity.md`, `policy.md`, `context.md`) is unioned with operator `tools.policy.protected_paths`, resolved once at agent start and immutable for the session. Denials raise `TOOL_PROTECTED_PATH` and emit `tool.protected_path.denied` (tool + caller DID + path).
- **Lethal-trifecta gate (REQ-010..016).** `SessionCapabilityLedger` + tag→leg map (`core/session_internal/capability_ledger.py`) accumulate `{private_data, external_comms, untrusted_input}` legs across calls and inject them as `PolicyContext.session_capabilities`; the trifecta forbidden set is passed into `build_pipeline(forbidden_compositions=...)`. `HumanGate` (`tools/human_gate.py`) pauses a trifecta-completing call for an operator-signed one-shot approval (never the agent DID — ASI09), fails closed on timeout/denial, and never auto-approves at federal. `[tools.human_gate]` config (timeout + named auto-approve compositions).
- **EgressProxy wiring (REQ-013).** One per-agent `EgressProxy` (deny-by-default `tools.policy.egress_allowlist`) is the single external-comms mediation point; a successful egress records the `external_comms` leg.
- **Sandboxed bash (REQ-020..025).** At enterprise/federal, `bash` delegates to arcrun's tier-routed isolation backend with the workspace bind-mounted read-write, protected files read-only (goal-lock survives sandboxing), and host `~/.arc`/`.audit` never mounted. Personal keeps host bash with an advisory goal-lock guard.

### Removed
- `ForbiddenCompositionChecker` (arcagent duplicate) — the subset check is now LIVE inside arctrust's `GlobalLayer`; arcagent supplies only the tag→leg mapping.

## [0.6.0] - 2026-07-06

SPEC-053: wire the operator key (audit authority) into every WORM sink; the agent DID seed no longer signs any audit chain.

### Changed
- The three WORM audit sinks are rewired to the deployment **operator key**, replacing the agent DID seed outright (no flag, no fallback): the policy-decision chain (`core/agent.py`), the skill-improver audit chain (`modules/skill_improver/_runtime.py`), and the new trace-checkpoint anchor (`core/model_manager.py`). Chains now verify only under the operator public key. **The mutated-skill signature stays on the agent DID** (SPEC-033 D3) — audit authority and artifact provenance are different attestations.
- `core/agent.py` loads the operator key read-only at startup from outside the workspace tool-sandbox (auto-bootstrapped at personal tier; vault seam for federal), and builds the federal external witness (tier = stringency; federal only *adds* the witness).

### Added
- `SecurityConfig` fields: `operator_key_dir`, `operator_vault_path`, `witness_mode`, `witness_log_url` (SPEC-053 REQ-004/005/010).
- `model_manager.build_checkpoint_sink` — operator-signed `trace.checkpoint` WORM anchor; at federal tier the head is also submitted to an external witness so a rollback past the last anchor is detectable even by a holder of the operator key.

## [0.5.0] - 2026-07-06

SPEC-033: enforce the Sign pillar on the workspace/agent-authored root — restricted-builtins load path, re-verify-at-load, TOFU first-load approval, signed self-modification tools, and a WORM-chained skill-improver audit. Scope is the untrusted workspace root only; first-party (builtins/global/per-agent) roots remain release-signed-upstream and out of scope.

### Added

- **Sidecar artifact signing** (`capabilities/artifact_signing.py`) — agent-authored capabilities get a detached `.arcsig` sidecar (content hash + Ed25519 signature, keyed to the agent's own DID) written on create/update. `create_skill`, `create_tool`, `update_skill`, and `update_tool` all sign what they write via the new `builtins/capabilities/_runtime.sign_artifact_file` helper. No-op when the agent has no signing identity.
- **Pluggable `TrustBackend`** (`capabilities/trust_backend.py`) — a one-method `verify()` Protocol the capability loader depends on instead of a concrete crypto call. `Ed25519TrustBackend` (arctrust, DID-scoped, network-free) is the default for self-authored artifacts; Sigstore keyless verification (arcskill) governs install-time hub skills separately.
- **Restricted-builtins module execution** (`tools/_dynamic_loader.build_restricted_builtins`) — the untrusted `<agent>/workspace/.capabilities/` root now executes under `RESTRICTED_BUILTINS` plus a denylist-enforcing `__import__`, in place of the prior bare `exec(code, module.__dict__)` with the full builtin surface. This is a fast-fail linter / defense-in-depth layer in front of the SPEC-036 execution sandbox — not a boundary, and not a substitute for it.
- **Load-time Sign gate** (`capabilities/capability_loader._passes_trust_gate`) — re-verifies the detached signature on every workspace-root load, independent of any install-time check, then adjudicates via `TofuLayer`: above personal tier a missing/invalid signature denies outright; first-sight and drift are TOFU decisions (`NEW_SIGHTING` / `DENY`). Any evaluation error denies — fail-closed.
- **`TofuLayer.approve_source`** (`core/tofu_layer.py`) — the pure data operation behind `arc trust approve`. Pins a capability name to its current source hash, superseding any prior approval so a re-approval after drift clears the `DENY`.
- **WORM signed hash-chain audit for skill mutations** — `skill_improver.CandidateStore.append_audit` now emits `skill.mutation.applied` through an injected `arctrust.AuditSink` (a `WormSink` in production, keyed to the agent's own identity) instead of writing a plaintext `audit.jsonl`. The mutated skill text is itself signed through the same sidecar convention `create_skill` uses.
- **New tests** — `tests/security/test_sign_gate_load.py`, `tests/security/test_workspace_restricted_load.py`, `tests/unit/capabilities/`, `tests/unit/modules/skill_improver/test_engine_signing.py`.

### Changed

- **`CapabilityLoader.__init__`** — gains `tofu`, `require_signature`, `trusted_public_key`, `trust_backend`, all defaulted off/`None` so a bare library loader keeps pre-SPEC-033 behavior. `agent_lifecycle.setup_capabilities` wires them from the agent's configured tier and identity (`require_signature` true at enterprise/federal).
- **`builtins/capabilities/_runtime.configure`** — takes an `identity` (arctrust `AgentIdentity`); new `sign_artifact_file()` helper signs artifacts on write using it.
- **`skill_improver._runtime.configure`** — takes an `identity`; resolves `(signer_did, signing_key)` and wires a `WormSink` at `<workspace>/.audit/skill_improver.worm` when the identity can sign. Fails open (audit disabled, module startup unaffected) if the sink can't be opened.

### Removed

- **`core/os_sandbox.py`** (and its test) — dead code: an uncalled OS-sandbox transport contract, never wired into the execution path. ASI05 enforcement is arcrun's tier-routed `execute` + backends (SPEC-036); this module ceded that ground and had nothing left to do.

### Security

- **SPEC-033 — Sign pillar enforced on the workspace/agent-authored root** — closes the gap where agent-authored code ran under a plain full-builtins `exec` with its signature (if any) checked only at install/create time, never re-checked at load. Restricted-builtins execution, verify-at-load, TOFU first-load approval, and signed self-modification tools now apply on every load. Scoped to the untrusted workspace root; first-party roots (builtins/global/per-agent) are release-signed-upstream and unaffected.

## [0.4.0] - 2026-04-26

Major refactor: identity primitives moved to arctrust, dedicated orchestration layer for spawn/sub-runs, four-pillar audit migration to arctrust sinks, and removal of legacy duplicate-named files cluttering the tree.

### Added

- **`arcagent.orchestration` package** — New layer between arcrun (pure loop) and the LLM-facing `delegate` tool. Owns `spawn`, `spawn_many`, `make_spawn_tool`, `RootTokenBudget`, `SpawnResult`, `SpawnSpec`, `TokenUsage`, and `SPAWN_GUIDANCE`. Spawn primitives no longer live in arcrun (`arcrun/builtins/spawn.py` removed). Concern split: arcrun runs one loop, `arcagent.orchestration` spawns sub-loops, `modules/delegate` wraps with policy + identity.
- **Voice / web modules consolidated** — Single `voice_module.py` and `web/url_policy.py` (cleanup of duplicated `*_module 2.py` siblings).
- **Vault audit-gap tests** — `tests/unit/modules/vault/test_resolver_audit_gap.py` and `test_vault_unreachable_audit_event.py` cover the four-pillar audit guarantees.
- **Identity-required tests** — `tests/unit/core/test_identity_required.py` enforces that `ArcAgent.__init__` requires a DID at every tier (ADR-019).
- **Personal-tier global-layer test** — `test_personal_tier_global_layer.py` verifies the policy pipeline still evaluates the global layer at personal tier.
- **Tier metadata test** — `test_tier.py` validates tier-stringency-not-gate semantics (ADR-019).
- **Tool registry DID enforcement test** — `test_tool_registry_did.py` confirms every dispatch carries `caller_did`.
- **UI reporter wiring test** — `test_ui_reporter_wiring.py` regression-tests the dashboard event hook.
- **Voice all-tiers audit test** — `test_voice_audit_all_tiers.py` verifies voice module audits at personal/enterprise/federal.
- **Web deny-by-default test** — `test_web_deny_by_default.py` confirms web module fails closed without explicit allowlist.

### Changed

- **Identity primitives moved to arctrust** — `core/identity.py` removed; `AgentIdentity`, `ChildIdentity`, `derive_child_identity`, `generate_did`, `parse_did`, `validate_did` now live in `arctrust.identity`. arcagent imports from arctrust. Eliminates the latent circular dependency documented in SPEC-018 §HIGH-1.
- **Trust store moved to arctrust** — `core/trust_store.py` and `utils/trust_store.py` removed; `load_operator_pubkey`, `load_issuer_pubkey`, `TrustStoreError`, `invalidate_cache` now in `arctrust.trust_store`.
- **Audit emission migrated to arctrust** — All security-relevant audit events now route through `arctrust.audit.emit(AuditEvent, sink)`. `JsonlSink` for compliance, `SignedChainSink` for tamper-evident chain, `arcui.bridge.UIBridgeSink` for live observability. Single emission point, sinks fan out per ADR-019.
- **Tool policy pipeline migrated to arctrust** — `core/tool_policy.py` shrunk from 614 LOC to a thin shim around `arctrust.policy.PolicyPipeline`. `Decision`, `PolicyLayer`, `ToolCall`, `PolicyContext`, `TierConfig`, `build_pipeline` all sourced from arctrust.
- **`ArcAgent.__init__` requires DID** — Identity is now mandatory at every tier, not just federal. Implements ADR-019 four-pillar universality.
- **`ToolRegistry` carries `caller_did`** — Every dispatch records the calling DID for the policy pipeline and audit trail.
- **Module-bus / extension API hardening** — Tighter typing across `module_bus.py`, `extensions.py`, `skill_registry.py`, `tool_registry.py`.
- **Browser, delegate, scheduler, planning, vault, voice, web modules** — Cleanup pass; legacy duplicate-named files removed; tighter audit emission paths.
- **README rewritten** — 385-line marketing prose replaced with focused layer-position + public-surface reference (under 100 lines).

### Removed

- **`core/identity.py`** — Migrated to arctrust. Re-export shim removed; callers must import from `arctrust`.
- **`core/trust_store.py`, `utils/trust_store.py`** — Migrated to arctrust.
- **Duplicate `* 2.py`, `* 2.yaml` files** — Cleanup of accidentally-checked-in macOS Finder duplicates across `delegate/`, `memory_acl/`, `user_profile/`, `voice/`, `web/`, `skill_improver/nudge/`, `tool_policy_layers 2.py`, `browser/`. No functional change.
- **`docs/voice-air-gap-setup 2.md`** — Stray duplicate doc.

### Security

- **ADR-019 Four Pillars Universal** — Identity, Sign, Authorize, Audit now enforced at every tier. Personal/enterprise/federal differ only in stringency (FIPS crypto, signed allowlists, layer count) — never in whether the pillar applies.
- **Audit single-point-of-emission** — All security events flow through `arctrust.audit.emit`; no module emits directly. Removes risk of schema drift across callers.

## [0.3.0] - 2026-04-18

Federal-first hardening: tool policy pipeline, dynamic tool surface with layered defense, unified proactive engine, Prometheus metrics, tier-aware self-modification. Implements SPEC-017.

### Added

- **Tool Policy Pipeline** (`core/tool_policy.py`) — 5-layer first-DENY-wins, fail-closed evaluator with LRU cache (p95 < 1ms @ 100 rules). Layers: Global → Provider → Agent → Team → Sandbox. Tier-aware `build_pipeline()` factory emits the correct stack per deployment (Federal=5, Enterprise=4, Personal=1). Shadow mode for safe rollout. Restricted mode when policy bundle stale.
- **Dynamic tool surface** (`tools/_decorator.py`, `tools/_dynamic_loader.py`, `tools/_egress.py`) — `@tool` decorator with type-hint schema inference; `DynamicToolLoader` pipeline: encoding check → 9-category AST validation → `RESTRICTED_BUILTINS` sandbox compile → registration. Origin-allowlisted egress proxy for dynamic tool network access.
- **Self-modification tools** (`tools/skill_tools.py`, `tools/tool_tools.py`, `tools/extension_tools.py`) — `create_skill`, `improve_skill`, `create_tool`, `create_extension`, `list_artifacts`, `reload_artifacts`. Tier gates: federal denies dynamic code; enterprise requires approval (audit-logged); personal allows. Every action emits a structured audit event.
- **Unified Proactive Engine** (`modules/proactive/`) — Replaces the legacy `pulse` + `scheduler` modules. Single asyncio task, min-heap priority queue, drift-free rescheduling (`last_actual_run + interval - overhead`), clock-warp detection, wake idempotency, heartbeat isolation (dedicated `HeartbeatContext` — no session state leak). `CircuitBreaker` (Resilience4j pattern) + `LeaderElection` Protocol with `NoOpLeaderElection` / `InMemoryElection` implementations. Timezone helper handles IANA zones + DST + overnight windows.
- **Prometheus metrics** (`core/metrics.py`) — In-process `MetricRegistry` with counters/gauges/histograms, text exposition format, and audit-sink adapters for policy and proactive events. Ships without `prometheus_client` dependency.
- **Capability-composition safety** — `ForbiddenCompositionChecker` rejects batches whose combined capability tags match a forbidden set (e.g. `file_read + network_egress = exfiltration`). Addresses non-compositional safety per arXiv:2603.15973.
- **`classification` on `RegisteredTool`** — Every tool declares `read_only` or `state_modifying`. All 7 built-ins annotated: `read/grep/find/ls` = `read_only`; `bash/edit/write` = `state_modifying`. Plus `capability_tags` for composition checks.
- **Adversarial test suite** — 42 tests under `tests/security/` covering AST bypass categories (CVE-cited), restricted-builtin enforcement, egress deny, capability composition. Designed to gate CI.
- **Runbook** — `docs/runbooks/spec-017-operations.md` — policy ops, scheduling, tier config, metrics wiring, incident response, legacy module migration.

### Changed

- **`ToolRegistry` dispatch** — When constructed with a `ToolPolicyPipeline`, every tool call flows through first-DENY-wins evaluation before reaching the tool's `execute()`. No sudo path. Pipeline is opt-in to preserve backward compatibility with existing deployments.
- **`ArcAgent._ensure_model`** — Wires `create_arcllm_bridge()` via the new `on_event` parameter on `load_eval_model()` so ArcLLM events (`llm_call`, `config_change`, `circuit_change`) now reach the Module Bus. Closes a long-standing integration gap.
- **`ArcAgent.shutdown`** — Closes the `httpx` client owned by the LLM model so connection pools are released deterministically.
- **Module loader** — Checks `enabled` BEFORE validating `entry_point`, allowing descriptor-only `MODULE.yaml` files (e.g. `vault/`) to coexist without breaking startup.
- **Messaging `ack` path** — Stores the real stream end-byte-offset in cursor so subsequent polls seek past consumed bytes (via new `StorageBackend.get_stream_end_byte_pos`). Replaces the prior `byte_pos=0` that forced full-stream rescans.
- **REPL `/sandbox` and `/strategy`** — Now mutate REPL state and emit `repl.sandbox_changed` / `repl.strategy_changed` audit events instead of printing help.

### Deprecated

### Removed

- **Legacy `modules/pulse/` module** — Functionality migrated to `modules/proactive/`. Per SPEC-017 R-040, no compat shim.
- **Legacy `modules/scheduler/` module** — Same migration path as `pulse`. The `arc agent schedule migrate` CLI command (to be shipped in a follow-up) handles persisted state migration.

### Fixed

- **`ui_reporter/MODULE.yaml`** — Ships with the package; now covered by a regression test.
- **Pre-existing test failures** unrelated to SPEC-017 — `freezegun` test dependency, missing `tomlkit` dep, `threading.Lock` isinstance check broken by Python 3.13, CDP client launch test, stale bio_memory/policy tests needing `session_id`.

### Security

- **OWASP LLM02 / ASI02 / ASI05 / ASI10** — addressed via tool policy pipeline (every tool call audit-logged with agent DID + rule ID), AST validator (9 bypass categories including the CVE-2023-37271 generator-frame bypass and the CVE-2025-68668 ctypes FFI bypass), and deny-by-default egress proxy.
- **NIST 800-53 SI-7(15), CM-5, CM-8** — Federal tier refuses dynamic tool / extension creation at the tool level, BEFORE the loader is consulted. Audit trail captures the denial.
- **Tamper-evident audit trail** — Every policy evaluation, self-modification action, circuit-breaker trip, and completion event emits a structured audit event with agent DID, rule ID, content hash, and timestamps.

## [0.2.0] - 2026-02-21

### Added

- **Biological memory module** — Long-term identity-aware memory system (`bio_memory/`). Tracks agent identity, episodic memory, and working memory across sessions. Includes:
  - `IdentityManager` — Persistent agent identity with traits, preferences, and behavioral patterns.
  - `WorkingMemory` — Session-scoped scratchpad for in-progress reasoning and intermediate state.
  - `Consolidator` — Promotes working memory to long-term episodic storage with relevance scoring.
  - `Retriever` — Context-aware memory retrieval with recency, relevance, and importance weighting.
  - `MODULE.yaml` — Declarative module manifest for Module Bus registration.
- **Shared text sanitizer** — `utils/sanitizer.py` provides `sanitize_text()` with NFKC normalization, zero-width character stripping, and control character removal. Centralizes ASI-06 (Memory & Context Poisoning) defense across all modules.
- **Bio memory CLI commands** — `arc agent bio_memory status|identity|episodes|working` for inspecting biological memory state.
- **Bio memory integration tests** — End-to-end tests for memory lifecycle (write, consolidate, retrieve) and retrieval accuracy.
- **Bio memory unit tests** — Component-level tests for identity manager, working memory, consolidator, retriever, and config.

### Changed

- **Entity extractor** — Refactored `_sanitize_fact_text()` to use shared `sanitize_text()` utility instead of inline implementation. Same defense, less duplication.
- **CLI agent commands** — Registered `bio_memory` as a lazy module group with `status`, `identity`, `episodes`, `working` subcommands.

### Security

- Centralized text sanitization prevents memory poisoning (OWASP ASI-06) with consistent NFKC normalization across entity extraction and biological memory.
- Biological memory validates all writes through the shared sanitizer before storage.

## [0.1.0] - 2026-02-01

### Added

- Initial release with core agent nucleus.
- Ed25519 cryptographic identity with W3C DID format.
- TOML-based configuration with Pydantic validation.
- OpenTelemetry traces, metrics, and structured audit events.
- Token-budgeted context manager with tiered compaction.
- Tool registry with schema validation, policy enforcement, and timeout guards.
- Event-driven module bus for extensibility.
- JSONL session persistence with retention policies.
- Markdown skill discovery and registration.
- Hot-loadable Python extensions.
- Runtime-mutable settings manager.
- Memory module with hybrid search (BM25 + vector), entity extraction, and policy engine.
- Sandboxed filesystem tools (bash, read, write, edit, ls, find, grep).
