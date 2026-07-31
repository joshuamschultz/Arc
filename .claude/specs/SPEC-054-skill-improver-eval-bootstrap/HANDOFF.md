# SPEC-054 Session Handoff — 2026-07-10

> **STATUS: SPEC COMPLETE.** All 4 phases done, all 18 tasks G1-G3 evidenced. Commits on
> `feature/SPEC-054-skill-improver-eval-bootstrap`: 333ecf0 (T-718..T-733), 1db2d1f (T-734
> arcui), 8c3bcff (T-735 E2E + the candidate-materialization fix it caught). Final matrix:
> arcskill 702 / arcagent 2446 / arccli 441 / arcstore 74 / arcui 508 (chat_ws pre-existing
> hang excluded) / E2E 5-of-5; ruff + format + mypy --strict clean. The mid-flight notes
> below are historical; remaining work = `/review SPEC-054` (follow-ups listed below) + merge.

> Written mid-Phase-3 in case of session pause. Supersedes/absorbs HANDOFF-2026-07-10-T724.md (single-task slice).

## Where things stand

**Branch**: `feature/SPEC-054-skill-improver-eval-bootstrap`
**Committed**: `333ecf0` — T-718..T-733 + production wiring (arcskill, arcagent, arccli, arcstore), all verified: arcskill 702 / arcagent 2446 / arccli 441 / arcstore 74 passed fresh; ruff + format clean; mypy --strict clean per package.
**Uncommitted**: T-734 (arcui evals/versions UI) was IN FLIGHT — files touched: `packages/arcui/{pyproject.toml, src/arcui/observe.py, routes/agent_detail/__init__.py, schemas.py, server.py}`, new `routes/agent_detail/skill_versions.py` + `tests/integration/test_skill_versions_routes.py`, `uv.lock`. If the agent didn't report completion, verify per the T-734 gates below before trusting.

## Task board

| Task | Status |
|---|---|
| T-718..T-722 (Phase 1 Foundation) | DONE, G1-G3 evidenced, committed |
| T-723..T-728 (Phase 2 Core) | DONE, G1-G3 evidenced, committed |
| T-729..T-733 + WIRE (Phase 3) | DONE, G1-G3 evidenced, committed |
| T-734 arcui UI | IN FLIGHT at handoff time |
| T-735 E2E + full matrix (Phase 4) | NOT STARTED |
| spec-compliance / task-validator / reflexion / README close | NOT STARTED |

## Resume checklist

1. `git status` — if arcui work looks complete, run its gates: `uv run pytest packages/arcui/tests -q`; ruff check + format --check packages/arcui; mypy strict per arcui pyproject. Commit arcui separately if green.
2. T-734 acceptance (PLAN): read-only eval-case + version-timeline routes over arcstore (`query.skill_versions` / `skill_candidate_body`, landed in 333ecf0), memoized server-side diff, confirm-gated audited rollback (ui.mutation, reject retired skills), StoreIngest `workspace_dir` wired in arcui Observe.
3. T-735 (Polish): E2E through the REAL path — create skill → use → suite generated (needs eval LLM configured) → mutation gated by generated anchors → classifier label lands with outcome_source='evaluator' → version in arcstore → rollback via CLI/UI — plus full 5-package matrix + "no placeholder eval file anywhere" + LOC budgets.
4. Then: spec-compliance + spec-task-validator skills, spec-reflexion, README phase notes 3+4 (use spec-readme scripts via the symlink root at the scratchpad readme-root — plugin scripts reject the uppercase SPEC- dir name), statuses, /review.

## Known follow-ups (not blockers)

- `.improver.lock` marker: CLI warns when present; the improver never WRITES it (single-flight is in-memory). Either wire marker writing in improver or drop the CLI check at /review.
- `reconcile_suppression` made fail-open around `adapter.retired_skills()` (test-driven loosening; warns loudly). Review at /review.
- Classifier is built even without an eval LLM (abstains); flag-on ≠ silently off, by design.
- Multi-skill turns: classifier receives only the single tracked `active_skill`; trace store keeps multiple spans. Acceptable v1; noted in Phase 2 note.
- Plugin `state_manager.py` expects checkbox PLAN format the generator doesn't emit — upstream plugin mismatch, task state tracked in session + phase notes instead.

## Key decisions made during implementation (see README Phase Notes for full list)

- `@generated` marker lives in module DOCSTRING (comments invisible to ast.parse); missing manifest entry = untrusted-machine (spoof defense); provenance strip on human edit = leave stale sha256 (do NOT remove the entry).
- create_skill writes NO evals file — empty evals/ dir so fail-closed no_suite_policy governs from birth.
- regen from CLI errors with "agent context" — generation seams live in the improver only.
- arcstore candidate keys are mtime-salted content keys (pure content keys break on rollback).
