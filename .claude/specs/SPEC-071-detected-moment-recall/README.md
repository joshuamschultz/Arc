# Specification: Spec 071 Detected Moment Recall

**Feature:** `SPEC-071-detected-moment-recall`
**Created:** 2026-08-20

## Status

| Doc | Status | Last Update |
|---|---|---|
| PRD | approved | 2026-08-20 |
| SDD | approved | 2026-08-20 |
| PLAN | approved | 2026-08-20 |
| Implementation | COMPLETE | 2026-08-20 |
## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md)
- Tech: [`../../steering/tech.md`](../../steering/tech.md)
- Structure: [`../../steering/structure.md`](../../steering/structure.md)
- Roadmap: [`../../steering/roadmap.md`](../../steering/roadmap.md)

## Decision Log Snippets

Cross-feature decisions referenced from [`../../decisions-log.md`](../../decisions-log.md):

_(none yet — link decisions as `[D-NNN](../../decisions-log.md#d-nnn)` when they apply to this feature)_

## Phase Notes

_(append `### Phase N: <name>` blocks via `append_phase_note.py` at phase boundaries during `/implement`.)_

## Learnings

Feature-specific insights captured here. Global / reusable patterns go to memory via `/memorize`.

- **Implementation COMPLETE** (2026-08-20). 18/18 tasks green; feature suite 88 passed; cross-package regression 596 passed; arcmemory no-arcagent-import + dependency-boundary architecture tests green; ruff + mypy --strict clean.
- **SDD naming error corrected:** the arcagent memory-module config class is `MemoryConfig` (in `modules/memory/config.py`), not the SDD's "MemoryModuleConfig".
- **Emission timing (verified):** `assemble_system_prompt` runs ONCE per run at `core/agent_dispatch.py:112`, BEFORE the existing `agent:pre_respond` emit at :137, and the loop runs with a fixed system prompt. So the user-turn `agent:moment` emit was placed BEFORE assembly (not at pre_respond) so the proactive buffer lands same-turn; `task_start` emits at the top of `_run_task` before `agent_run_fn`. The tasks `_State` gained a `bus` field (lifecycle `select_for` supplies it).
- **Detector fix (T-983):** `entity_seen` matches cue↔entity on WORD TOKENS (so cue "nebula" hits entity "Nebula 0"); whole-name-only matching silently never fired on multi-word names.
- **decision_point gated OFF by config** (`proactive_decision_point`, default False): `agent:pre_plan` is mid-loop with no re-assembly, so it cannot inject same-turn (A3 deferred) — enabling it would only add audit noise.

## Open Questions

- **Proactive recall is subsumed by query recall on live user turns** (found by the T-990 journey test): query recall already fires on the same text and `_merge_recall` dedupes the proactive block, so on user turns proactive recall's only net-new effect is the `memory.recall_attributed` audit (trigger). Its unique prompt-content payoff is on query-less assemblies (`task_start`). Decide whether that is acceptable, or whether proactive recall should fire on cues NOT already in the literal message (working-context entities) to add unique value on user turns. Follow-up, not a bug.
