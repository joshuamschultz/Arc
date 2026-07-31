# Implementation Plan: Skill-Improver Eval Bootstrap (SPEC-054)

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- **T-718** (red): Provenance model + tier-aware gate: failing tests
  - domain: test
  - Components: COMP-002
  - Requirements: REQ-109
  - Acceptance: RED: tests assert EvalCase carries machine_authored/origin from the @generated header + manifest cross-check (model-emitted marker without manifest entry is untrusted); enterprise/federal gate counts only human-authored toward min_golden_cases while personal counts all; placeholder-only suites classify as empty (no_suite_policy applies).
- **T-719** (green): Provenance model + tier-aware gate: implementation
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-110
  - Acceptance: GREEN: the paired RED-phase tests pass; provenance detected in load_suite's existing single AST pass (header-bounded); evalgate min-cases check split tier-aware; tier flows from construction; ruff/mypy clean.
- **T-720** (red): SuiteConfig sub-block + toggle precedence/audit: failing tests
  - domain: test
  - Components: COMP-003, COMP-008
  - Requirements: REQ-112
  - Acceptance: RED: tests assert extra='forbid' rejects misspelled keys; frontmatter override tightens only; global-off dominates per-skill-on with OVERRIDDEN audit; exempt tags cannot re-enable under global off; every flip emits config_change (from/to/actor/timestamp) before effect, at-most-once; frontmatter binds at mutation-unit start.
- **T-721** (green): SuiteConfig + toggle/audit layer: implementation
  - domain: backend
  - Components: COMP-003, COMP-008
  - Requirements: REQ-113, REQ-114
  - Acceptance: GREEN: the paired RED-phase tests pass; SuiteConfig sibling of LifecycleConfig inside ImproverConfig forwarded from [modules.skills.improver.suite]; toggle snapshot resolved once per pass; D-444-style audited disable; ruff/mypy clean.
- **T-722** (green): create_skill scaffold replacement (delete placeholder, fail-closed birth state)
  - domain: backend
  - Components: COMP-005
  - Requirements: REQ-105
  - Acceptance: RED test first proving a placeholder-bearing suite currently bypasses no_suite_policy; GREEN: create_skill writes no evals file (empty evals/ dir only), placeholder scaffold deleted in the same edit, generate_on_create schedules async generation; new skill at enterprise/federal is governed by fail-closed no_suite_policy from birth.

## Phase 2: Core

- **T-723** (red): SuiteGenerator adoption cascade: failing tests
  - domain: test
  - Components: COMP-001
  - Requirements: REQ-102, REQ-103
  - Acceptance: RED: with deterministic fake LLMInvoker + fake EvalRunner, tests assert the ordered cascade (ast.parse -> anti-tautology reject of assert-free/assert-True/input-only bodies -> N=5 sandbox runs -> mutation probe); all-pass cases adopted as anchors in evals/test_golden_generated.py; current-bundle failures quarantined and never gate-admitted; oracles sourced from Contract text; atomic writes.
- **T-724** (green): SuiteGenerator: implementation
  - domain: ai-workflow
  - Components: COMP-001
  - Requirements: REQ-101, REQ-104
  - Acceptance: GREEN: the paired RED-phase tests pass; generation via injected LLMInvoker only (provider-free); candidate budget + early-stop at min_cases anchors; discard stats + per-outcome audit events; quarantine recorded in harness-written manifest; ruff/mypy clean.
- **T-725** (red): Trigger wiring + per-skill single-flight: failing tests
  - domain: test
  - Components: COMP-004
  - Requirements: REQ-108
  - Acceptance: RED: tests force real interleaving (Barrier/Event, never instant mocks) proving generation and optimization on one skill serialize; lazy trigger fires inside _optimize when suite empty/placeholder-only; post-mutation extension is add-only (existing anchors byte-identical); sweep backstop early-exits when nothing pending and cannot double-claim a lazily-triggered skill.
- **T-726** (green): Trigger wiring + single-flight: implementation
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-106, REQ-107
  - Acceptance: GREEN: the paired RED-phase tests pass; reuses _spawn/_guarded/semaphore; per-skill asyncio locks; sweep pass added to existing Curator loop with jitter + most-used-first ordering; config snapshot at spawn; aclose drains in-flight generations.
- **T-727** (red): Turn-end outcome classifier: failing tests
  - domain: test
  - Components: COMP-006
  - Requirements: REQ-116
  - Acceptance: RED: tests assert heuristic pre-filter gates LLM calls (no-signal turns cost zero LLM calls); classifier binds failure only to an explicitly named skill else abstains; silence never labels failure; positive labels require safety-check pass; label reaches adapter.on_turn_end(outcome=...) off the respond path; multi-skill turns split credit using per-skill error counts.
- **T-728** (green): Turn-end outcome classifier: implementation
  - domain: ai-workflow
  - Components: COMP-006
  - Requirements: REQ-115
  - Acceptance: GREEN: the paired RED-phase tests pass; classifier lives in arcagent.modules.skills (eval LLM via get_eval_model, bounded transcript window); zero arcskill API change; outcome_source='evaluator' now produced end-to-end; toggleable via COMP-008 bool; ruff/mypy clean.

## Phase 3: Integration

- **T-729** (red): Arg capture + trace promoter: failing tests
  - domain: test
  - Components: COMP-007
  - Requirements: REQ-117
  - Acceptance: RED: tests assert args scrubbed at the observe boundary BEFORE persistence (raw secrets never on disk); federal tier stays hash-only regardless of config; evaluator-success traces promote to deterministic replay anchors (volatile fields canonicalized) and failures to repro cases pinned to skill_version; promoted cases enter the adoption cascade with machine provenance; obsolete cases retire visibly.
- **T-730** (green): Arg capture + trace promoter: implementation
  - domain: backend
  - Components: COMP-007
  - Requirements: REQ-118
  - Acceptance: GREEN: the paired RED-phase tests pass; capture config-gated per skill; reuses sanitize_text scrub path; promoter writes to the compact case store, never rescans the full trace corpus at replay time; ruff/mypy clean.
- **T-731** (red): arc skill evals CLI (list/regen/edit): failing tests
  - domain: test
  - Components: COMP-009
  - Requirements: REQ-119
  - Acceptance: RED: tests assert list is a static AST walk (no imports/execution); edit validate-on-save rejects unparseable saves and re-opens preserving edits; commits are temp-file + os.replace; warnings fire before reducing passing-anchor count or dropping below min_golden_cases; human edit strips @generated; regen shows diff + confirms before overwrite.
- **T-732** (green): arc skill evals CLI: implementation
  - domain: backend
  - Components: COMP-009
  - Requirements: REQ-111
  - Acceptance: GREEN: the paired RED-phase tests pass; follows existing skill.py subparser/_SUBCOMMAND_MAP/_print_table conventions; $VISUAL then $EDITOR precedence with git-style abort semantics; nonzero exit on rejected save.
- **T-733** (green): arcstore candidate/version ingestion
  - domain: db
  - Components: COMP-010
  - Requirements: REQ-120
  - Acceptance: RED test then GREEN: tailer ingests candidate_store manifest/candidates/audit via per-file byte cursor; rows content-hash keyed with INSERT OR IGNORE idempotency; WORM chain verified at ingest with stored verified flag; manifest-present/store-absent candidates queryable as pending.
- **T-734** (green): arcui evals view + version timeline/diff/rollback
  - domain: ui
  - Components: COMP-010
  - Requirements: REQ-120
  - Acceptance: RED route tests then GREEN: read-only per-skill eval-case view and lineage-DAG timeline (active badged); lazy server-side word-level diff memoized on content-hash pairs; metadata-only list payloads; rollback confirm-gated, warns scores are historical, emits one ui.mutation AU-3 event, wired to improver.rollback; 200 empty-OK vs 503 unreadable; pruned candidates render tombstones.

## Phase 4: Polish

- **T-735** (green): E2E through the real path + full quality gates
  - domain: test
  - Components: COMP-001, COMP-002, COMP-003, COMP-004, COMP-005, COMP-006, COMP-007, COMP-008, COMP-009, COMP-010
  - Requirements: REQ-105, REQ-115
  - Acceptance: E2E exercising the REAL wiring (producers-unwired defense): create skill -> use it -> suite generated -> mutation gated by generated anchors -> classifier label lands in persisted trace with outcome_source='evaluator' -> version visible in arcstore -> rollback via CLI/UI path -> every step present on the WORM chain. Full package matrix: pytest, ruff check, ruff format, mypy --strict green across arcskill/arcagent/arccli/arcstore/arcui; LOC budgets checked; no placeholder eval file remains anywhere in the repo.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-101 | T-724 |
| REQ-102 | T-723 |
| REQ-103 | T-723 |
| REQ-104 | T-724 |
| REQ-105 | T-722, T-735 |
| REQ-106 | T-726 |
| REQ-107 | T-726 |
| REQ-108 | T-725 |
| REQ-109 | T-718 |
| REQ-110 | T-719 |
| REQ-111 | T-732 |
| REQ-112 | T-720 |
| REQ-113 | T-721 |
| REQ-114 | T-721 |
| REQ-115 | T-728, T-735 |
| REQ-116 | T-727 |
| REQ-117 | T-729 |
| REQ-118 | T-730 |
| REQ-119 | T-731 |
| REQ-120 | T-733, T-734 |

## Open Questions

_(none)_
