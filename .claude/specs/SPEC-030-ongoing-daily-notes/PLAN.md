# SPEC-030 — PLAN: Ongoing Daily Notes

**Status**: VERIFIED (post-review) | 4-reviewer swarm; fixes applied for sanitizer-hardening/DRY, dropped rollup_compacts_source (ordering hazard+double-count), longterm recall (keep-newest), full-backlog drain, UTC clock, audit. ruff 0, mypy 0, arcagent 3312 passed.
TDD mandatory. Single module (`arcagent/modules/memory`) + memory config. No cross-module tasks.

Legend: `[ ]` pending · `[x]` done · maps `REQ-NNN`.

---

## Phase 0 — Characterize + decouple

- [x] **T-001** [test] Assert current state: a compaction produces no note write, and
  `agent:pre_compaction` has no live subscriber after removal. `test_memory_no_compaction_coupling`.
- [x] **T-002** [impl] Delete `memory_pre_compaction` hook (`capabilities.py`) + the legacy
  `bus.subscribe("agent:pre_compaction")`/`_on_pre_compaction` (`markdown_memory.py`). REQ-001.
- [x] **T-003** [verify] `grep -ri pre_compaction modules/memory` returns nothing; suite still green.

## Phase 1 — Tier 1: per-turn raw append

- [x] **T-010** [test] After one turn (`memory_post_respond`), today's `notes/<date>.md` has a raw
  line; NO eval-model call occurs on that path (mock model, assert not called). REQ-002.
- [x] **T-011** [impl] Add a no-LLM raw-append helper; call it in `memory_post_respond` before
  staging. Truncated single-line format. Gated by `raw_capture_enabled`. REQ-002/008.
- [x] **T-012** [test] Crash-safety: raw line present even if the background loop never runs
  (don't start the task). REQ-002.

## Phase 2 — Tier 2b: session-end consolidation

- [x] **T-020** [test] On `agent:shutdown`, exactly one eval-model pass runs over today's notes;
  output is sanitized; fail-open leaves raw appends intact on model error. REQ-004/007.
- [x] **T-021** [impl] Extend `memory_shutdown` with a single consolidation pass (dedupe/tidy),
  sanitized rewrite, `hook_active`-guarded, gated by `session_consolidation_enabled`. REQ-004/007/008.

## Phase 3 — Tier 3: lazy new-day rollup

- [x] **T-030** [test] First `_ensure_daily_notes` of a new day with an un-rolled prior day
  enqueues exactly one background rollup and writes a `.rolled` marker; a second event does not
  re-roll (idempotent). Works when "today" advances (inject date). REQ-005/006.
- [x] **T-031** [impl] In `_ensure_daily_notes`, detect new-day + un-rolled prior file →
  `spawn_background(rollup(prev))`. Gated by `daily_rollup_enabled`. REQ-005.
- [x] **T-032** [impl] `rollup(prev)`: eval-model consolidate → sanitize → append to the long-term
  sink read by `inject_memory_sections` → write `.rolled` marker (append-then-mark); optionally
  compact source (`rollup_compacts_source`). REQ-005/006/007/008.
- [x] **T-033** [test] Rollup output is recalled: after a rollup, `inject_memory_sections` surfaces
  the consolidated long-term content. REQ-005.
- [x] **T-034** [test] Crash mid-rollup (no marker) → prior day retried once next new-day event, no
  duplicate long-term entry. REQ-006.

## Phase 4 — Config + close

- [x] **T-040** [impl] Add `raw_capture_enabled` / `session_consolidation_enabled` /
  `daily_rollup_enabled` / `rollup_compacts_source` to `MemoryConfig`; each tier honors its flag.
  REQ-008.
- [x] **T-041** [test] Each flag off removes its behavior; all-off degrades to current
  create-file + background-extract only. REQ-008.
- [x] **T-042** [verify] `git diff --stat` touches only `modules/memory/**` + `core/config.py`
  (REQ-009). `ruff` 0, `mypy --strict` 0, arcagent suite green; fix any pre-existing debt in
  touched files. REQ-010. Update status PENDING→COMPLETE, commit with the change.

---

## Coverage matrix

| REQ | Tasks | REQ | Tasks |
|-----|-------|-----|-------|
| 001 | T-001,002,003 | 006 | T-030,032,034 |
| 002 | T-010,011,012 | 007 | T-020,021,032 |
| 003 | (unchanged; T-010 asserts no regression) | 008 | T-011,021,031,040,041 |
| 004 | T-020,021 | 009 | T-042 |
| 005 | T-030,031,032,033 | 010 | T-042 |

Every REQ maps to ≥1 task. No task edits outside `modules/memory` + memory config.

## Branch

`feat/SPEC-030-ongoing-daily-notes` off `develop` (now == `main`). Small, self-contained;
lands independently of SPEC-029.
