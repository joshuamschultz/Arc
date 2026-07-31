# SPEC-044 — Best skill builder/improver — PLAN

**Status:** COMPLETE (all phases 0–9 landed; AC-1..7 green)
**Branch:** `feat/SPEC-044-skill-improver`
**Traces:** PRD REQ-001..070 / NFR-001..006; SDD §2–§8
**Method:** TDD (RED → GREEN → REFACTOR). No production code without a failing test first. Full per-package matrix + `ruff` + `mypy --strict` + core-LOC check at every phase boundary.

> **THE recurring failure is producers-unwired** ([[feedback_producers_unwired_pattern]]): a mechanism ships correct but its activating wiring is dead, and a rigged fixture test passes anyway. Every phase below that adds a mechanism carries an **E2E-through-real-path** acceptance criterion driving it through the actual extension + hook + sandbox + sign + audit path — never a direct optimizer call. Feature inventory (SDD §4.1) maps every deleted arcagent file to its arcskill destination; **every feature maps to a task**.

---

## Phase 0 — Scaffold & feature inventory (relocation skeleton)

> **⚠️ RESOLVED 2026-07-08 (package ruling per Josh — FINAL).** An intermediate revision pivoted this plan to a standalone `arcevolve` sibling package; Josh ruled that was a misread: **"arcskill IS the optional package; arcagent already manages skills (writes, loads, etc); arcskill supercharges them."** The original target stands: **`packages/arcskill/src/arcskill/improver/`** (subpackage beside `hub/`). The Phase-0 scaffold already committed there (9dd00af) is VALID — do not move or delete it. No `packages/arcevolve/` may exist.

- [x] **T0.1** Create the `arcskill.improver` subpackage under `packages/arcskill/src/arcskill/improver/` (arcskill already has pyproject + the SPEC-039 `mypy --strict` gate; arctrust already a dep). *(done in 9dd00af)*
- [x] **T0.2** Write the **feature-inventory** table (every file in `arcagent/modules/skill_improver/` → move | delete | absorb-into-extension). Commit it in this PLAN's README as the migration map.
- [x] **T0.3 (RED)** Architecture test: `arcskill.improver` imports **no** `arcagent`/`arcllm`/`arcmemory` (REQ-004); it MAY use sibling `arcskill.hub`/arctrust. *(done in 9dd00af)*
- **Gate:** arch test green at the **arcskill.improver** location; inventory complete (every feature mapped).

## Phase 1 — Move pure logic to arcskill.improver (behavior-preserving relocation)

- [x] **T1.1 (RED→GREEN)** Moved `models/config/guardrails/candidate_store/pareto` into `arcskill.improver`; 108 ported unit tests pass UNCHANGED. `SkillImproverConfig`→`ImproverConfig` (own `extra=forbid` base). Local `_util.py` replaces `arcagent.utils` io/sanitizer deps (REQ-004).
- [x] **T1.2** Moved `engine/evaluator`, `reflector`→`mutate` (prose path preserved). Injected `LLMInvoker`/`Signer` seams (`seams.py`) replace direct eval-model + `arcagent.capabilities.artifact_signing`. Ported tests w/ fake + arctrust-backed signer seams. *(GEPA trace-reflection D-1c and richer Mutator/Judge/EvalRunner Protocols land Phases 3-4.)*
- [x] **T1.3** Moved `nudge/`→`arcskill.improver.nudge` (create-nudge preserved); `startup()` bus subscription removed (→ Phase-2 extension), bus injected via constructor. 66 nudge unit tests + FP-rate integration ported with duck-typed ctx fake.
- [x] **T1.4** **DELETED** `arcagent/modules/skill_improver/` (17 files, 3149 LOC). Rewired 2 residual arcagent refs to the new `skills` module (`_WORM_SINK_MODULE_NAMES`, `_prior_audit_chains_exist` → `skills.worm`). Module-facade/tools/trace_collector/worm/operator-split tests deleted with the module; re-established in Phase 2 (extension) / Phase 7 (audit split).
- **Gate:** ✅ arcskill 558 pass/5 skip (217 improver) green; arcagent NCLOC **DOWN 2881** (develop 33549 → 30668, NFR-001); core 3498/<3500; arcskill.improver ruff + mypy-strict clean; arcagent suite 2072 pass after deletion; arch test green. *(committed 532ced6)*

## Phase 2 — arcagent seam + wiring (mirror `arcagent/brain/`)

- [x] **T2.1 (RED)** AC-1 via the REAL hook path: default `adapter='none'` → `NullSkillAdapter`, `state().active is False`, every hook short-circuits, **zero files** under the workspace. Plus a live-path test proving the `arcskill` adapter actually persists traces through the hooks (anti-producers-unwired).
- [x] **T2.2 (GREEN)** `arcagent/skilladapt/protocol.py` — `SkillAdapter` Protocol + `NullSkillAdapter` (mirrors `brain/protocol.py`). Both Null + `ArcSkillImprover` satisfy it structurally.
- [x] **T2.3** `skilladapt/select.py` — `none`→Null, `arcskill`→lazy `ArcSkillImprover` (not-installed → Null, never crash), dotted BYO. **BYO signing gate**: unsigned class-path refused at enterprise/federal (fail-closed), allowed at personal. 6 select tests green.
- [x] **T2.4** Wiring lives in `modules/skills/` (config + `_runtime` + `capabilities`) — mirrors `modules/memory/` (the discoverable/configurable mechanism; `[skills.improver]` naming realized as `[modules.skills]` + nested `improver`). Hooks: `agent:post_tool`→observe (+ skill-read detection, the split-off trace_collector half), `agent:post_plan`→on_turn_end, `agent:pre_respond`→maybe_improve, `agent:ready`→index. Injected seams: eval LLM (`get_eval_model`), agent-DID `_SidecarSigner`, operator WORM `skills.worm`. Proactive→review_lifecycle deferred to Phase 6 (sweep is a no-op now). EvalRunner (arcskill.hub.dry_run, DC-5) lands Phase 3.
- **Gate:** ✅ AC-1 green through the real turn path; 10 skilladapt tests green; core 3498/<3500; arcagent NCLOC still down (31103 vs 33549); arcagent suite 2494 pass; ruff + mypy-strict clean (arcskill.improver 17 files, skilladapt+skills 7 files).

## Phase 3 — Golden-task eval gate (the hard gate)

- [x] **T3.1** `evalgate.py`: `load_suite` (AST-discovers `evals/test_*.py::test_*`) + **strict-improvement** gate (`EvalGate.decide`) — accept iff ≥1 previously-failing case flips to pass AND none regress (SkillOpt strict `>`). Regressing candidate rejected even with higher judge score (REQ-022). `EvalRunner` seam added; `EvalCase`/`EvalOutcome`/`BundleView` models added; `min_golden_cases` config (OQ-3). 8 gate unit tests + 5 facade tests.
- [x] **T3.2** No-suite policy (`no_suite_policy`, REQ-021): code blocked every tier; prose blocked at enterprise/federal, personal audit-warn. **Wired into the real optimize path** (`ArcSkillImprover._gate` runs before `apply_result`; judge demoted to ranking) — 5 facade tests drive the wired gate (anti-producers-unwired).
- [x] **T3.3** `sandbox_runner.HubEvalRunner` — default `EvalRunner` over `arcskill.hub` (public async `execute_in_sandbox` + per-skill `timeout_s` + opt-in Docker `mount`). Stdlib golden-task harness (no pytest binary in the minimal sandbox image); per-case parse. Real-Docker integration test proves fail→fix; fed/ent fail-closed; personal host-fallback. Wired as the default in `ArcSkillImprover`.
- [x] **T3.4 (E2E)** AC-3 (no-suite → code mutation blocked) covered by the code-path gate (`evalgate` code-kind min-cases + no-suite policy); the full code-repair E2E is AC-2 (Phase 4).
- **Gate:** ✅ eval-gate decides acceptance (judge ranks only), wired into the real optimize path; concrete sandbox runner live (real Docker); strict-improvement + tier no-suite policy green.

## Phase 4 — Code-repair mutation (the headline)

- [x] **T4.1 (RED)** `BundleView`/`BundlePatch` value types + `mutate.py` code path: from failing traces (`error_type`) propose a multi-file patch to script code (REQ-010/011). Test with a fake `Mutator` returning a known patch.
- [x] **T4.2** Apply path: write patch → `Signer.sign_bundle` (agent DID, all bundle files) → **hub re-verify** + scan → reload (REQ-012/016). Test re-verify rejects an unsigned/mismatched bundle (fail-closed).
- [x] **T4.3 (E2E, the big one)** **AC-2**: real agent run exercises a skill with a **seeded code bug** → usage accrues via hooks → improver proposes a code patch → patch **passes the golden-task suite in the sandbox** → bundle re-signed (agent DID) → re-verified through hub gate → reloaded → the previously-failing golden task now passes. Assert the whole chain fired via the production extension, not a direct `engine.optimize` call.
- **Gate:** AC-2 green E2E; prose path (T1.2) still green.

## Phase 5 — Change-bound (SkillOpt) [DEEPEN]

- [x] **T5.1 (RED)** `ChangeBound.check(patch, seed, tier, skill_override)` layered on existing guardrails (REQ-030/033). Test: a patch exceeding `max_lines_changed`/`max_files_touched` is rejected **before** evaluation, with an audit event (**AC-4**).
- [x] **T5.2** Config surface (`[skills.improver.change_bound]`) + per-tier/per-skill resolution `min(tier_ceiling, skill_override)`, federal floor non-relaxable (REQ-031). Test personal-relax vs federal-floor.
- [x] **T5.3** Mark `max_lines_changed`/`max_ast_distance` values **[DEEPEN]** — /deepen pins from the SkillOpt paper. Placeholder floors from PRD §7 until then.
- **Gate:** AC-4 green E2E; bounds enforced pre-eval.

## Phase 6 — Nudge → usage → retire lifecycle

- [x] **T6.1 (RED)** `lifecycle.py` state machine + usage stats (REQ-041): success/failure/partial rate, last-used turn, error signatures accrue from traces.
- [x] **T6.2** **improve-nudge** (REQ-042): underperformer → improve-nudge + enqueue gated improvement. Reuse existing dedup/cooldown.
- [x] **T6.3** **retire** (REQ-043): unused-past-window OR below floor after N attempts → disable + retain lineage (reversible, never delete). **revive** (REQ-044) restores from lineage.
- [x] **T6.4** Transition audit (REQ-045/050): every edge emits an **operator-signed** `LifecycleEvent`; federal requires operator approval for retire/revive. *(Review-closure C-2: the D-10 approval gate is now real — `Approver` seam gates retire/revive/mutation; fail-closed when required-but-unwired.)*
- [x] **T6.5 (E2E)** **AC-5**: real inactivity sweep retires a skill; operator revive restores it; both are operator-signed audit events observed on the WORM chain. *(Review-closure C-1, reworked per team ruling: the sweep runs on a canonical `@background_task` loop — `skills_review_lifecycle_loop` mirroring memory's consolidate loop — NOT the dormant ProactiveEngine (deferred to SPEC-042); `tests/integration/test_lifecycle_sweep_e2e.py` drives the loop's poll body → `review_lifecycle` → retire → registry suppression → operator-signed WORM audit. No facade call, DC-10.)*
- **Gate:** AC-5 green E2E through the `@background_task` producer; state machine transitions all audited + operator-gated per tier.

## Phase 7 — Signing/audit authority split + reversibility (SPEC-053/033)

- [x] **T7.1 (RED)** **AC-6**: assert mutation + lifecycle audit events are signed by the **operator key** while the mutated bundle sidecar is **agent DID**. Test both keys distinctly.
- [x] **T7.2** Rollback (REQ-052): restore prior bundle → re-verify → cooloff → operator-signed audit. Port + extend existing rollback test.
- [x] **T7.3** Confirm bash/subprocess (eval runner, patch write) confined off `~/.arc/operator/**` + `.audit/**` (SPEC-035/053) — no path to forge the trail.
- **Gate:** AC-6 green; audit authority provably independent of the audited subject.

## Phase 8 — Optional arcmemory enrichment + builder handoff

- [x] **T8.1** `maybe_improve(insight=...)` consumes optional `Brain.retrieve` insight text passed as a primitive (REQ-060). Test: memory-less (NullBrain) path fully works; with a fake Brain, insight reaches the `Mutator` prompt. arcskill imports no arcmemory (arch test).
- [x] **T8.2 (S)** Builder handoff (REQ-070): skills authored via the build path ship a golden-task eval scaffold so they are improvable from creation. Test a freshly-built skill has a runnable (empty-but-valid) suite.
- **Gate:** enrichment optional + arch-clean; new skills are improvable.

## Phase 9 — Hardening, LOC, docs, truthfulness

- [x] **T9.1** Coverage: line ≥80%, branch ≥75%, core-component ≥90% on `arcskill.improver` + `arcagent.skilladapt`.
- [x] **T9.2** **AC-7**: record arcagent NCLOC delta (must be **down**); core `<3500`; full package matrix **including arcskill (with its new `improver/` subpackage)** + cross-package run green; ruff + mypy-strict clean everywhere (fix any pre-existing errors surfaced — CLAUDE.md).
- [x] **T9.3** Truthful docs: arcskill README + `[skills.improver]` config docs reflect reality; version bumps (arcskill minor, arcagent minor). No claim without wired code.
- [x] **T9.4** Update this spec's README status → COMPLETE **in the same commit** as the final implementation ([[project_spec_status_sync_requirement]]).
- **Gate:** all ACs (1–7) green E2E; quality gates pass; ready for review.

---

## Feature inventory — migration map (REQ-001, no-legacy)

| arcagent file (delete) | LOC | Destination |
|---|---|---|
| `modules/skill_improver/models.py` | 271 | `improver/models.py` (+ `BundlePatch`, `EvalCase/Outcome`, `LifecycleEvent`) |
| `.../config.py` | 48 | `improver/config.py` (+ change_bound, lifecycle) |
| `.../guardrails.py` | 121 | `improver/guardrails.py` (+ `ChangeBound`) |
| `.../candidate_store.py` | 193 | `improver/candidate_store.py` |
| `.../pareto.py` | 123 | `improver/pareto.py` |
| `.../engine.py` | 266 | `improver/engine.py` (rewired: eval-gate, code path) |
| `.../evaluator.py` | 202 | `improver/` behind `Judge` seam |
| `.../reflector.py` | 133 | replaced by `improver/mutate.py` (prose + code) |
| `.../trace_collector.py` | 281 | split: signal-extraction → `skilladapt/extension.py`; storage/analysis → arcskill.improver |
| `.../nudge/*` | 634 | `improver/nudge.py` (+ improve-nudge); bus subscription → extension |
| `.../capabilities.py` | 290 | replaced by `skilladapt/extension.py` hooks |
| `.../skill_improver_module.py` | 399 | replaced by `ArcSkillImprover` (arcskill.improver) + `skilladapt` wiring |
| `.../_runtime.py` | 179 | dissolved (state held by `ArcSkillImprover` instance) |
| **arcagent net** | **~3,149 down** | **arcskill up** (new `improver/` subpackage — logic) + small `skilladapt` in arcagent (Protocol + Null + thin wiring) |

## Risks & mitigations

- **Producers-unwired** → every mechanism phase has an E2E-through-real-path AC (AC-2/3/4/5/6); no phase closes on a rigged-fixture test alone.
- **Sandbox** → `EvalRunner`'s default impl wraps **`arcskill.hub.dry_run`** (in-package to arcskill; DC-5 corrected the "SPEC-036 VmBackend" naming); it is a seam for test fakes; fail-closed at ent/federal via `SandboxRequired`; personal degrades to host with audit-warn.
- **arcskill provider-freedom** → injected `Mutator/Judge/EvalRunner` seams; arch test forbids arcagent/arcllm/arcmemory imports.
- **LOC regression in arcagent** → measured at every gate; relocation must net-reduce (NFR-001).
- **Sub-agent destructive git in shared tree** → sequential work on `feat/SPEC-044-skill-improver` or worktree isolation; commit before any parallel worker ([[feedback_subagents_no_destructive_git]]).
