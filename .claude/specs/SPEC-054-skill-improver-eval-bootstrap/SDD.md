# Solution Design Document: Skill-Improver Eval Bootstrap (SPEC-054)

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

Two coupled additions to the SPEC-044 skill improver, extending existing seams without changing arcskill's public API. Half 1 makes the golden-task gate real: a SuiteGenerator produces machine-verified pytest golden cases for any skill, provenance-marked and tier-counted, triggered event-driven with a sweep backstop. Half 2 gives the improver a true outcome signal: an arcagent-side turn-end classifier fills the existing producer-less on_turn_end(outcome=...) parameter, and a config-gated trace promoter turns labeled traces into replay/repro cases. All surfaces (CLI evals management, arcui version timeline) are read/edit projections over existing on-disk stores. Tier posture per the Four Pillars: every mechanism verifies, authorizes, audits, and attributes at every tier ([tech.md#security-standards](../../steering/tech.md#security-standards)).

## Architecture

Follows the layer DAG in [structure.md#layer-model](../../steering/structure.md#layer-model). arcskill (surface layer) owns generation, provenance, gating, and promotion — all provider-free behind the existing LLMInvoker/EvalRunner/Signer seams. arcagent owns the thin wiring: the outcome classifier (it holds the transcript and eval LLM), create_skill scaffolding, config forwarding, and trigger hooks. arccli/arcui/arcstore own read/edit surfaces via the established pull pattern (arcstore file-tailer -> arcui query layer, never push). New flows: (1) maybe_improve -> [suite missing?] -> SuiteGenerator.generate -> validation cascade in HubEvalRunner sandbox -> adopt/quarantine -> gate proceeds; (2) agent:pre_respond -> heuristic pre-filter -> classifier LLM (escalation only) -> on_turn_end(outcome) -> TraceStore._finalize (outcome_source='evaluator'); (3) candidate_store/manifest -> arcstore ingest (byte cursor) -> arcui timeline/diff/rollback.

## Components

### COMP-001: SuiteGenerator (arcskill.improver.suitegen)
**Responsibility:** Generate candidate golden cases from SKILL.md Contract/Examples/Validation sections + script files via one bounded LLMInvoker call; run the adoption cascade: ast.parse -> anti-tautology AST check -> N=5 sandbox runs (HubEvalRunner) -> optional mutation probe; write adopted anchors atomically (temp + os.replace), quarantine failures to a manifest-tracked quarantine area. Oracles grounded in declared Contract text (anti-bug-freezing); run-inferred expectations flagged change-detector.
**Dependencies:** LLMInvoker seam, HubEvalRunner, COMP-002, COMP-003
**Inputs:** skill_name, BundleView (SKILL.md text + scripts), SuiteConfig
**Outputs:** GenerationResult {adopted: list[EvalCase], quarantined: list[QuarantinedCase], discard_stats}; adopted cases written to evals/test_golden_generated.py; audit event per outcome

### COMP-002: Provenance model (EvalCase + evalgate tier-aware count)
**Responsibility:** machine_authored flag on EvalCase, detected from the @generated header token in the same AST pass load_suite already runs, cross-checked against a harness-written manifest entry (model can never self-assert provenance). EvalGate splits min_golden_cases tier-aware: enterprise/federal count human-authored only; personal counts all. Human edit detection strips the marker and reclassifies.
**Dependencies:** evalgate.load_suite, CandidateStore manifest
**Inputs:** evals/ tree + suite manifest + constructed tier
**Outputs:** list[EvalCase] with provenance; GateDecision honoring tier-aware human minimum

### COMP-003: SuiteConfig ([modules.skills.improver.suite])
**Responsibility:** Pydantic sub-block, extra='forbid', sibling of LifecycleConfig/ChangeBoundConfig inside ImproverConfig: autogen, min_cases, max_cases, generate_on_create, extend_after_mutation, candidate_budget, flake_runs. Per-skill override via the existing frontmatter improver: block; values snapshot at task spawn (tier-through-construction).
**Dependencies:** ImproverConfig
**Inputs:** [modules.skills.improver.suite] TOML block + frontmatter override
**Outputs:** validated SuiteConfig instance bound at construction

### COMP-004: Trigger wiring + single-flight (ArcSkillImprover extensions)
**Responsibility:** Lazy trigger inside _optimize (suite empty/placeholder-only -> generate before gating); post-mutation add-only extension spawned via existing _spawn/_guarded/semaphore; Curator sweep backstop pass (most-used-first, jitter, early-exit predicate); per-skill single-flight asyncio locks covering generation AND optimization so the gate never reads a suite mid-write and event/sweep paths cannot double-claim.
**Dependencies:** COMP-001, ArcSkillImprover, skills module @background_task loop
**Inputs:** maybe_improve/apply-mutation/sweep events
**Outputs:** at-most-one in-flight generation or optimization per skill; bounded by Semaphore(max_concurrent)

### COMP-005: create_skill scaffold replacement (arcagent.builtins)
**Responsibility:** Delete the test_placeholder scaffold. create_skill writes NO evals file synchronously (fail-closed no_suite_policy governs from birth — closes the placeholder bypass); when generate_on_create is enabled it schedules async generation via COMP-004. Placeholder-only suites encountered anywhere are treated as empty by COMP-002.
**Dependencies:** COMP-004, no_suite_policy
**Inputs:** create_skill(name, description, triggers, tools, body)
**Outputs:** skill folder with empty evals/ dir; optional queued generation task; no assert-True files ever written

### COMP-006: Turn-end outcome classifier (arcagent.modules.skills.outcome)
**Responsibility:** Cheap heuristic pre-filter (correction/re-ask/negative-signal regex+lexicon over the bounded transcript window) runs every turn; only candidate turns escalate to one eval-LLM call with an explicit dissatisfaction-taxonomy rubric that must name the responsible skill or abstain. Silence defaults success-or-abstain; positive labels pass a safety check. Label forwarded via the existing on_turn_end(outcome=...) parameter — zero arcskill API change; fires after respond, never blocking.
**Dependencies:** eval LLM (get_eval_model), skills capabilities hooks, COMP-008 toggle
**Inputs:** turn transcript window + active skill spans + per-skill error counts
**Outputs:** outcome label in {success, failure, partial, ''} with skill attribution, delivered to adapter.on_turn_end

### COMP-007: Arg capture + trace promoter (arcskill.improver)
**Responsibility:** Config-gated arg capture at the observe() boundary, scrubbed via the existing sanitize path BEFORE persistence; hash-only remains the non-overridable federal default (SI-12(2)). Promoter distills evaluator-labeled successes into deterministic replay anchors (volatile fields canonicalized) and observed failures into repro cases, pinned to skill_version, stored in the compact case store with an expiry/quarantine path — never auto-re-blessed.
**Dependencies:** TraceStore, COMP-001 adoption cascade, COMP-002 provenance
**Inputs:** SkillTrace stream with capture config + outcome labels
**Outputs:** promoted replay/repro EvalCases (machine-authored provenance) entering the same adoption cascade

### COMP-008: Toggle + audit layer (skills module config)
**Responsibility:** Layered toggles resolved once at construction/pass boundary: adapter master switch (exists), outcome_classifier bool, suite autogen bool, per-skill frontmatter, exempt tags. Deterministic deny-wins precedence with OVERRIDDEN audit reason codes; every flip emits config_change (from/to/actor/timestamp) before taking effect, at-most-once (the arcllm D-444 pattern); frontmatter reads bind at mutation-unit start (ASI06 defense).
**Dependencies:** SkillsConfig, arctrust audit sinks
**Inputs:** config state + frontmatter + flip events
**Outputs:** immutable per-pass toggle snapshot; audited config_change events on the WORM chain

### COMP-009: arc skill evals CLI (arccli.commands.skill)
**Responsibility:** evals <name> (list cases + provenance + latest gate result via static AST walk, never executing), evals regen <name> (diff + confirm before overwrite), evals edit <name> (temp copy -> $VISUAL/$EDITOR -> re-parse -> reject-and-reopen preserving edits -> atomic commit; warns before reducing passing-anchor count or dropping below min_golden_cases; strips @generated marker on human edit). Follows existing subparser/_SUBCOMMAND_MAP/_print_table conventions.
**Dependencies:** COMP-002, evalgate.load_suite, arcskill lock (atomic write)
**Inputs:** arc skill evals <verb> <name> [args]
**Outputs:** table/diff output; validated atomic writes to evals/; nonzero exit on rejected save

### COMP-010: arcui version + evals surface (arcstore ingest + arcui routes)
**Responsibility:** arcstore tailer ingests candidate_store manifest/candidates/audit (byte-cursor incremental, content-hash keyed, verify-on-ingest). arcui adds read-only per-skill routes: eval-case list, version timeline from the manifest lineage DAG (badge active), lazy server-side word-level diff memoized on hash pairs, confirm-gated rollback emitting one ui.mutation AU-3 event and warning that target scores are historical. Discovery-free (operator facade only), metadata-only list payloads.
**Dependencies:** arcstore ingest, CandidateStore, improver.rollback, arcui audit taxonomy
**Inputs:** GET /skills/{name}/evals, /skills/{name}/versions, /versions/diff?a=&b=; POST rollback {candidate_id, confirm}
**Outputs:** JSON timelines/diffs (200 empty-OK, 503 unreadable); audited rollback via existing non-destructive manifest flip


## Data Model

EvalCase gains machine_authored: bool (default False) + origin: {generated|promoted|human}. New suite manifest (evals/.manifest.json, harness-written): per-file provenance, quarantine list, adoption stats, generation timestamps — atomic writes. QuarantinedCase {source_file, nodeid, reason, created_at, skill_version}. SkillTrace unchanged except ToolCallRecord.args (optional, scrubbed, config-gated; absent at federal). SuiteConfig/OutcomeConfig Pydantic models (extra='forbid'). arcstore: new candidate/version tables keyed by content hash with verified flag. No schema change to MutationEvent or the WORM chain format.

## External Integrations

None external. Internal seams only: LLMInvoker (generation + classifier), HubEvalRunner sandbox (Firecracker/Docker per tier, fail-closed above personal), arctrust audit sinks (WORM), arcstore tailer, existing HumanGate approval ladder for gated ops. Threat mapping (required by [tech.md#security-standards](../../steering/tech.md#security-standards)): ASI05 — generated test code executes only inside the tier sandbox; ASI06 — frontmatter toggle reads bind at mutation-unit start, suite manifest harness-written; LLM01/05 — generated case content sanitized + AST-validated before any write; LLM06 — classifier can only label, never mutate; LLM10 — candidate budgets, semaphore caps, cheap-filter-first ordering; ASI10 — every generation/label/flip/rollback audited to the WORM chain.

## Traceability

| Requirement | Components |
|---|---|
| REQ-101 | COMP-001, COMP-004 |
| REQ-102 | COMP-001 |
| REQ-103 | COMP-001, COMP-002 |
| REQ-104 | COMP-001 |
| REQ-105 | COMP-005 |
| REQ-106 | COMP-004 |
| REQ-107 | COMP-004 |
| REQ-108 | COMP-004 |
| REQ-109 | COMP-002 |
| REQ-110 | COMP-002 |
| REQ-111 | COMP-002, COMP-009 |
| REQ-112 | COMP-003 |
| REQ-113 | COMP-008 |
| REQ-114 | COMP-008 |
| REQ-115 | COMP-006 |
| REQ-116 | COMP-006 |
| REQ-117 | COMP-007 |
| REQ-118 | COMP-007 |
| REQ-119 | COMP-009 |
| REQ-120 | COMP-010 |

## Alternatives Considered

Considered every-X-turns polling for generation (rejected: turn count uncorrelated with skill usage; event+backstop hybrid matches existing memory/scheduler precedents). Considered synchronous suite generation inside create_skill (rejected: LLM latency on the create path; fail-closed no-suite policy is the safer birth state — and any placeholder fallback re-opens the no_suite_policy bypass). Considered trusting LLM-emitted @generated markers (rejected: marker spoofing; harness writes provenance to a manifest the model never controls). Considered auto-promoting quarantined failing generated cases (rejected: inverse bug-freezing — an LLM-invented failing test can encode a wrong expectation; only trace-derived repros or human-reviewed cases enter the gate as failing targets). Considered putting the outcome classifier in arcskill (rejected: arcskill is provider-free and never sees transcripts; arcagent already holds the eval LLM and the existing on_turn_end parameter). Considered coverage-based adoption gating (rejected: 100% coverage with ~4% mutation score is documented; mutation probe is the discriminating filter).

## Risks and Mitigations

Bug-freezing: generated oracles encode actual behavior — mitigated by Contract-grounded oracles and change-detector labeling (COMP-001). Marker spoofing — harness-written manifest provenance (COMP-002). Classifier mislabel steering improvement — abstain default, explicit attribution requirement, safety-checked positives, aggregation thresholds before any rewrite triggers (COMP-006). Generation/gate race — per-skill single-flight (COMP-004). Cost blowout — candidate budget + semaphore + cheap-filter ordering + heuristic pre-filter escalation (COMP-001/006). Silent suite shrink on bad edit — validate-on-save with reject-and-reopen (COMP-009). Audit blinding by the toggle that disables auditing's producer — flip event fired before downgrade, at-most-once (COMP-008).

## Open Questions

_(none)_
