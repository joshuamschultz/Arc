# SPEC-044 HANDOFF — Phases 0–3 done, resume at Phase 3.3

*Written by the orchestrator from the Phase 0–3 worker's reports (worker hit session limit before writing this itself). Verify anything load-bearing against the code.*

## State (verified by orchestrator 2026-07-08)

Branch `feat/SPEC-044-skill-improver`, clean tree, 4 commits:
- `9dd00af` P0 scaffold — arcskill.improver subpackage + provider-free arch test
- `532ced6` P1 relocation — all improver logic arcagent → `arcskill.improver` (no-legacy delete of `arcagent/modules/skill_improver/`, 17 files / 3,149 LOC; arcagent NCLOC DOWN 2,442: 33,549→31,107)
- `86fad3e` P2 — `arcagent/skilladapt/` (SkillAdapter Protocol + NullSkillAdapter + select w/ BYO signing gate, mirrors `brain/`) + `arcagent/modules/skills/` thin wiring (config+_runtime+capabilities, mirrors `modules/memory`); AC-1 proven through REAL hook path
- `2593c51` P3 — `evalgate.py` strict-improvement golden gate (≥1 fail→pass, no regression, ties rejected) + tier no-suite fail-closed policy, wired into `ArcSkillImprover._optimize` BEFORE apply (judge demoted to ranking)

Gates all green (independently verified): arcskill 571 pass/5 skip; arcagent unit 2,494 pass/8 skip; ruff clean; mypy --strict clean; LOC budgets ALL PASS — **core at 3,498/3,500 (only 2 lines headroom — put new code in arcskill or arcagent modules/, NEVER core)**.

## Layout

- `packages/arcskill/src/arcskill/improver/`: `improver.py` (facade `ArcSkillImprover` — the SkillAdapter-shaped adapter, select value `"arcskill"`), `engine.py`, `evalgate.py`, `evaluator.py`, `mutate.py`, `guardrails.py`, `candidate_store.py`, `pareto.py`, `models.py`, `config.py`, `seams.py`, `trace_store.py`, `_util.py` (local replacement for arcagent.utils), `nudge/`
- Tests: `packages/arcskill/tests/{unit,integration,architecture}/improver/` (108 ported tests pass unchanged + new ones; arch test = no arcagent/arcllm/arcmemory imports)
- `packages/arcagent/src/arcagent/skilladapt/`: `protocol.py`, `select.py` (lazy import, BYO signing gate)
- `packages/arcagent/src/arcagent/modules/skills/`: thin config + `_runtime` + capabilities

## Known stubs / staged work (from the P0–3 worker)

- `review_lifecycle` is a **no-op stub** (P6 builds the real lifecycle state machine)
- `MutationEvent` still uses the candidate_store **placeholder actor** (`did:arc:skill-improver`) — P7 replaces with operator-key-signed emission (WORM sink already built in `modules/skills/_runtime`)
- Seams staged: P1 shipped thin `LLMInvoker`+`Signer` (Judge=SkillEvaluator, Mutator=SkillReflector); richer `Mutator`/`Judge` over `BundleView` arrive with P4; `EvalRunner` seam exists (P3) but concrete `hub.dry_run` per-case runner is **P3.3 — gate is fail-closed until then**
- Module-level tests deleted with the old module (trace_collector/facade/tools/worm/operator-split) — security-relevant ones (worm, operator-split) MUST be re-established by end of P7 with an explicit list in the P7 report

## Remaining phases (PLAN.md is authoritative)

- **P3.3** concrete EvalRunner over `arcskill.hub.dry_run` (Firecracker federal / Docker fallback; `SandboxRequired` fail-closed; thread per-skill timeout override — default 10s too tight). **Docker IS available on this machine (29.4.0)**: AC-2 must drive the REAL Docker path, plus prove the fail-closed branch (sandbox forced-unavailable) AND personal-tier host fallback.
- **P4** code-repair (BundlePatch multi-file + `Signer.sign_bundle` agent-DID + hub re-verify + reload) + **AC-2 E2E through the production extension path** (the headline; no rigged fixtures, no direct `engine.optimize` calls)
- **P5** ChangeBound — Josh LOCKED: Lt personal=8/enterprise=4/federal floor=2, cosine decay, rejected-edit buffer, strict-improvement gate; per-skill override within tier ceiling; `max_ast_distance` is a regularizer NOT a security gate; tier flows through construction (federal-stamps-federal audit assertion) + AC-4
- **P6** lifecycle state machine + Curator usage-sweep — Josh LOCKED: 30-day default window, ALL sweep settings adjustable in config.toml; auto-merge deferred + AC-5
- **P7** operator-key/agent-DID audit split + AC-6 + re-establish deleted security tests
- **P8** memory insight enrichment (already threaded) + builder eval scaffold (REQ-070)
- **P9** coverage (≥80/75/90) / docs / version bumps / README status → COMPLETE

## Accepted deviations (orchestrator-ruled, log stays)

1. Config key: `[modules.skills]` + nested improver dict (NOT literal `[skills.improver]`) — follows `[modules.memory]` precedent.
2. GEPA+SkillOpt = ONE integrated method in a single `optimize()` pass (D-1c, Josh-confirmed).
3. DC-5: EvalRunner wraps `arcskill.hub.dry_run`; do NOT touch `arcrun/sandbox.py` (policy checker, not executor).
4. Package ruling FINAL: `arcskill.improver`; arcskill IS the optional supercharger; NO arcevolve.
