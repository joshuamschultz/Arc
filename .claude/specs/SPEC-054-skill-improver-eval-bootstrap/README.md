# Specification: Spec 054 Skill Improver Eval Bootstrap

**Feature:** `spec-054-skill-improver-eval-bootstrap`
**Created:** 2026-07-10

## Status

| Doc | Status | Last Update |
|---|---|---|
| PRD | approved | 2026-07-10 |
| SDD | approved | 2026-07-10 |
| PLAN | approved | 2026-07-10 |
## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md)
- Tech: [`../../steering/tech.md`](../../steering/tech.md)
- Structure: [`../../steering/structure.md`](../../steering/structure.md)
- Roadmap: [`../../steering/roadmap.md`](../../steering/roadmap.md)

## Decision Log Snippets

Cross-feature decisions referenced from [`../../decisions-log.md`](../../decisions-log.md):

_(none yet — link decisions as `[D-NNN](../../decisions-log.md#d-nnn)` when they apply to this feature)_

## Phase Notes

### Phase 1: Foundation

_Recorded 2026-07-10_

T-718..T-722 complete (G1-G3 evidenced). Surprises: (1) @generated marker must live in the module DOCSTRING — comments are invisible to ast.parse, so the RED author moved the marker spec; (2) reclassification-on-human-edit needs no separate API — manifest hash mismatch inside load_suite covers REQ-111; (3) one pre-existing SPEC-044 test (test_evalgate fixture) used assert-True bodies as fixture data and rotted under the new placeholder rule — fixture updated to real assertions; (4) plugin state_manager.py expects checkbox-format PLAN tasks that plan-generator does not emit — task state tracked in-session instead. SuiteConfig defaults chosen where spec was silent: max_cases=10, candidate_budget=20, generate_on_create=True, extend_after_mutation=True.

### Phase 2: Core

_Recorded 2026-07-10_

T-723..T-728 complete (G1-G3: arcskill 681 passed, arcagent 2440 passed, ruff+mypy strict clean). Surprises: (1) two seam shapes reconciled — concrete SuiteGenerator (COMP-001, generate(name, view)->GenerationResult) vs improver trigger seam SuiteTrigger (COMP-004, generate(*, skill_name, skill_dir, kind)); production adapter wiring deferred to Phase 3 (arcagent _runtime) to avoid file collision with T-728. (2) Post-mutation extend gating on autogen is load-bearing: personal-tier no-suite prose auto-accept would otherwise fire extends with autogen off. (3) Classifier constructed even without eval LLM (abstains fail-open) — flag-on must not silently disable. (4) reconcile_suppression made fail-open around adapter.retired_skills() (test-driven loosening; real failures warn loudly — review at /review). (5) Positive praise is a pre-filter candidate signal — it is the only source of success credits; policy-risk praise (bypass/skip x sandbox/policy) downgrades to abstain (jailbreak-praise defense). NOTE for Phase 3: arcagent production wiring of SuiteGenerator->SuiteTrigger adapter + sweep_suites hookup into run_lifecycle_sweep still unwired (producers-unwired risk — T-735 E2E must exercise it).

### Phase 3: Integration

_Recorded 2026-07-10_

T-729..T-734 + WIRE complete (G1-G3: arcskill 702, arcagent 2446, arccli 441, arcstore 74, arcui 508+26 — all fresh; ruff+mypy strict clean x5 packages). Committed 333ecf0 + arcui commit. Surprises: (1) REQ-111 provenance strip = LEAVE the stale manifest sha256 (removing the entry would make the file MORE machine-trusted under spoof defense). (2) CLI regen correctly errors 'agent context' — generation seams live improver-side only. (3) arcstore pure content-derived keys break on rollback (prior state re-inserted is dropped by INSERT OR IGNORE) — keys salted with manifest mtime_ns. (4) green-T734 went idle WITHOUT a completion report — work verified complete by direct gate-running (empty report != failed task, but never trust silence). (5) arcui full suite has a pre-existing chat_ws hang (known deferred issue) — suite verified with that file excluded. Follow-ups for /review: .improver.lock never written by improver (CLI-side check only); reconcile_suppression fail-open loosening; classifier single-active-skill limitation.

### Phase 4: Polish + Close

_Recorded 2026-07-10_

T-735 complete; spec COMPLETE. THE E2E EARNED ITS KEEP: caught a real producers-unwired gap all 11 unit tests missed — SuiteGenerator never materialized candidate source into the sandbox bundle (in-memory-only cases can never pass), so generation silently adopted nothing and personal-tier prose mutations applied UNGATED via no_suite_policy audit-warn. Root cause: the unit-test fake classified current-vs-mutant by BundleView equality, encoding the broken assumption. Fix (8c3bcff): _with_candidate overlay before every cascade run (mutant probe poisons original THEN overlays); fake reclassified by poison marker + new materialization-invariant assertion. Final matrix: arcskill 702, arcagent 2446, arccli 441, arcstore 74, arcui 508 (pre-existing chat_ws hang excluded), E2E 5/5; ruff + format clean repo-wide; mypy --strict clean all five packages; no placeholder scaffolds in any src. Compliance recorded inline (spec-task-validator expects checkbox PLAN format this generator does not emit): REQ-101..104 test_suitegen+test_suite_triggers+E2E stage2; REQ-105 create_skill tests+E2E stage1; REQ-106..108 test_suite_triggers (Barrier-forced interleaving); REQ-109..111 test_provenance+CLI strip test; REQ-112..114 test_suite_config+test_toggles; REQ-115/116 test_outcome (24)+E2E stage5 (outcome_source=evaluator persisted); REQ-117/118 test_arg_capture+test_promoter; REQ-119 test_cli_skill_evals (18); REQ-120 arcstore ingest (8)+arcui routes (26)+E2E stage6. All 20 REQs evidenced. Commits: 333ecf0, 1db2d1f, 8c3bcff on feature/SPEC-054-skill-improver-eval-bootstrap.

## Learnings

Feature-specific insights captured here. Global / reusable patterns go to memory via `/memorize`.

_(none yet)_

## Open Questions

_(none yet)_
