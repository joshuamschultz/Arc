# SPEC-030 — PRD: Ongoing Daily Notes

EARS-format requirements. Every requirement carries an acceptance criterion tied to ≥1
principled-coder pillar. Scope is the arcagent `modules/memory` package only.

---

## Decouple from compaction

### REQ-001 — Notes never triggered by compaction [Must] · Modularity
The system SHALL remove the `memory_pre_compaction` hook and any `agent:pre_compaction`
subscription, so daily notes are never written or nudged as a result of context compaction.
**AC**: `grep pre_compaction` in `modules/memory` returns nothing; a compaction (SPEC-029
`SessionManager.compact`) produces no note write. Note-taking and compaction share no code path.
Source: research (MemGPT/sleep-time anti-pattern).

---

## Tier 1 — Continuous crash-safe capture

### REQ-002 — Per-turn raw append, no LLM [Must] · Simplicity, Scalability
WHEN a turn completes (`agent:post_respond`), the system SHALL append a short, plain-text line
to today's notes file **immediately and without any LLM call** (timestamp + a truncated
user/assistant marker), before any background enrichment runs.
**AC**: after one turn, today's `notes/<date>.md` contains a raw line for that turn even if the
process is killed before the background loop's next iteration; no model call occurs on the
`post_respond` path for this write. Latency added to the turn is a file append only.
Source: "ASSUME INTERRUPTION" (Anthropic memory tool); LangMem hot-path critique (no LLM per turn).

### REQ-003 — Background enrichment preserved [Should] · Scalability
The system SHALL keep the existing `entity_extraction_loop` background task as the off-critical-path
enrichment layer (eval-model extraction), unchanged in its latency-isolation property.
**AC**: entity extraction still runs on the interval loop, not on the hot path; the raw append
(REQ-002) does not depend on it. Source: Letta sleep-time (latency isolation).

---

## Tier 2b — Session-end consolidation

### REQ-004 — Consolidate on session end [Must] · Simplicity
WHEN the agent shuts down (`agent:shutdown`) or a session closes, the system SHALL run **one**
lightweight eval-model pass to dedupe/clean the raw appends accumulated in today's notes,
in place. This is the guaranteed consolidation floor for short/one-shot runs.
**AC**: after a one-shot run ends, today's notes are deduped/cleaned by exactly one model pass;
if consolidation fails it fails open (raw appends remain, run still exits cleanly).
Source: Reflexion (per-episode), Anthropic harness (update at session end).

---

## Tier 3 — Lazy day-end rollup

### REQ-005 — Rollup previous day on new-day boundary [Must] · Scalability
WHEN today's notes file is first created and a previous day's file exists that has not yet been
rolled up, the system SHALL spawn a **background** consolidation of that previous day into
long-term memory and mark it rolled (so it runs at most once). This SHALL fire for both a
long-lived daemon crossing midnight and a next-day one-shot invocation.
**AC**: given yesterday's notes exist un-rolled, the first `_ensure_daily_notes` of a new day
enqueues exactly one background rollup of yesterday and marks it rolled; a second new-day event
does not re-roll. Runs off the interactive path. Source: Generative Agents (daily reflection),
practitioner nightly-rollup, dual-mode gap the research flagged as unsolved.

### REQ-006 — Rollup is idempotent and bounded [Must] · Scalability
The rollup SHALL be idempotent (a "rolled" marker prevents re-processing) and SHALL NOT block
startup or the first turn of the new day.
**AC**: marker persists across restarts; a crash mid-rollup leaves the day eligible for one retry,
never a partial-duplicate long-term entry.

---

## Cross-cutting

### REQ-007 — Consolidation via arcllm, sanitized [Must] · Security, Modularity
All consolidation/rollup LLM calls SHALL go through the arcllm eval model, and all model output
SHALL be sanitized (NFKC, zero-width/control-char strip, length cap) before it is written to any
notes/memory file.
**AC**: no provider SDK import in the memory module; rollup/consolidation output passes through the
existing sanitizer; zero-width/control chars are stripped. Source: ASI-06/LLM-05.

### REQ-008 — Configurable, secure defaults [Should] · Security
Each tier SHALL be individually toggleable under `[memory]` in `arcagent.toml`, with defaults that
are safe and cheap (raw capture on; session/day consolidation on; no unbounded growth).
**AC**: disabling a tier via config removes its behavior with no code change; defaults produce
bounded file growth (rolled days archived/compacted, not accumulated forever).

### REQ-009 — Module-only, no boundary bleed [Must] · Modularity
The change SHALL touch only `arcagent/modules/memory` (+ its config). No arcrun, no arcllm, no
core compaction code.
**AC**: `git diff --stat` touches only `modules/memory/**` and `core/config.py` (memory config).

### REQ-010 — Quality gates [Must] · all pillars
`ruff check` = 0, `mypy --strict` = 0, arcagent suite green, new tests per tier. Any pre-existing
lint/type error in touched files fixed (CLAUDE.md "leave it correct").
**AC**: fresh gate output in /verify.

---

## Out of scope (explicit)

- Importance/salience scoring per event (Generative Agents' LLM-scored importance) — YAGNI for v1;
  the new-day + session-end boundaries are sufficient triggers. Revisit if quiet-day tuning needed.
- A wall-clock heartbeat timer (safety net for a single very long turn) — deferred; the 1.0s
  background loop already bounds intra-turn loss.
- Cross-day long-term memory *retrieval* changes (`inject_memory_sections` stays as-is).
- Touching the legacy `MarkdownMemoryModule` class beyond removing its dead `pre_compaction`
  subscription.
