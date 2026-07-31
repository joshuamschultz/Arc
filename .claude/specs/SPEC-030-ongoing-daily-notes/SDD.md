# SPEC-030 — SDD: Ongoing Daily Notes

Design filtered Simplicity → Modularity → Security → Scalability. Everything lives in
`arcagent/modules/memory`; nothing crosses into arcrun/arcllm/core-compaction.

---

## 0. Shape

```
agent:post_respond ──▶ Tier 1: raw append (NO LLM)  ──▶ notes/<today>.md
                   └─▶ stage pending_messages ──▶ [background] entity_extraction_loop (Tier 2, eval model)

_ensure_daily_notes (new day) ──▶ Tier 3: spawn_background rollup(yesterday) ──▶ long-term memory + mark rolled

agent:shutdown ──▶ Tier 2b: one eval-model consolidation pass over today's raw appends (dedupe/clean)

(compaction) ──✗── no path to notes   [REQ-001]
```

Trigger axis (research): **capture = per-event, cheap, on-path; consolidation = boundary-gated,
LLM, background.** Boundaries used: turn (capture), session-end (consolidate), new-day (rollup).
Never token pressure.

---

## 1. Remove compaction coupling (REQ-001)

- Delete `memory_pre_compaction` (`capabilities.py:263-274`) and its `@hook(event="agent:pre_compaction")`.
- Delete the legacy `bus.subscribe("agent:pre_compaction", ...)` + `_on_pre_compaction` in
  `markdown_memory.py` (class is uninstantiated, but leave no dead subscriber).
- No `agent:pre_compaction` emit is added anywhere (reverses the discarded "re-emit from
  maybe_compact" idea). Notes and compaction are fully independent.

## 2. Tier 1 — per-turn raw append (REQ-002)

In `memory_post_respond`, before staging for background extraction, append one plain line to
today's file via `NoteManager` (append-only). No model call.

- Format: `- HH:MM:SSZ · user: <first ~120 chars> → assistant: <first ~120 chars>` (truncated,
  single line). Deterministic, cheap, greppable.
- Uses the existing append-only enforcement (`NoteManager.enforce_append_only` already guards
  `notes/` writes). The write is a direct file append, not a tool call, so it is not subject to
  the tool-write hook re-entrancy.
- **Crash-safety**: this is the durable record if the process dies before the 1.0s background loop
  runs or before session-end. It is the "ASSUME INTERRUPTION" layer.

**Simplicity**: one short helper, no LLM, no new state. **Scalability**: O(1) file append per turn;
no per-turn model cost (the anti-pattern the research rejects).

## 3. Tier 2 — background enrichment (REQ-003, unchanged)

`entity_extraction_loop` (interval 1.0s) keeps draining `pending_messages` and writing extracted
entities. Untouched — it already provides the off-critical-path enrichment (Letta sleep-time
property). Tier 1 does not depend on it; they are additive.

## 4. Tier 2b — session-end consolidation (REQ-004)

Extend the existing `agent:shutdown` hook (`memory_shutdown`) — after draining background tasks —
to run one eval-model pass that dedupes/cleans today's raw appends in place:

- Read today's notes, send to the eval model with a light "dedupe and tidy these notes, preserve
  every distinct fact/decision, drop repetition; keep it a flat bulleted list" prompt.
- Sanitize output (§7), rewrite today's file (append-only contract relaxed *only* for this
  owner-driven consolidation, guarded by `hook_active` re-entrancy flag).
- **Fail-open**: on any error, keep the raw appends and exit cleanly (a one-shot run must still
  terminate). One pass only — no loop.

This is the floor: a one-shot CLI run with no new-day boundary still gets consolidated at exit.

**Simplicity**: reuses the shutdown hook + eval model + sanitizer already present.

## 5. Tier 3 — lazy new-day rollup (REQ-005, REQ-006)

In `_ensure_daily_notes` (the function that creates today's file):

```
if today's file does NOT exist yet:      # first turn of a new day (daemon or one-shot)
    create today's file (existing behavior)
    prev = most recent notes/<date>.md with date < today AND no ".rolled" marker
    if prev exists:
        spawn_background(rollup(prev))    # off the interactive path
```

`rollup(prev)`:
1. Read `prev`; eval-model consolidate into a durable long-term summary.
2. Sanitize (§7); append to the long-term memory sink (the existing memory store / a
   `notes/_longterm.md` roll — reuse whatever `inject_memory_sections` reads so rollups are
   actually recalled).
3. Write a sibling marker `notes/<prevdate>.rolled` (idempotency — REQ-006). Optionally compact
   `prev` to its summary to bound growth (REQ-008).

- **Idempotent**: the `.rolled` marker gates re-processing; a second new-day event or a restart
  finds it already rolled. A crash *before* the marker leaves `prev` eligible for exactly one
  retry (marker is the last write) — no partial-duplicate long-term entry because the append +
  marker are ordered append-then-mark and the consolidation is a pure function of `prev`.
- **Dual-mode**: fires for a daemon crossing midnight (today's file first-created then) and for a
  one-shot run the next day (same first-create path). No live scheduler required.
- Uses the existing `spawn_background` + runtime semaphore for bounded concurrency.

**Scalability**: rollup is O(one day) off-path, at most once per day per prior day; markers keep
it bounded. **Modularity**: pure memory-module concern.

## 6. Config (REQ-008)

Extend `MemoryConfig` (`modules/memory/config.py`):

```
raw_capture_enabled: bool = True          # Tier 1
session_consolidation_enabled: bool = True # Tier 2b
daily_rollup_enabled: bool = True          # Tier 3
rollup_compacts_source: bool = True        # bound file growth
```

Each tier checks its flag and no-ops when off (feature toggles via config, not code branches).
Defaults: cheap capture on, both consolidations on, growth bounded.

## 7. Security (REQ-007)

- All model output (session consolidation, rollup) passes the existing sanitizer (NFKC,
  zero-width strip, control-char strip, length cap) before any file write — the same ASI-06/LLM-05
  defense SPEC-029 applied to the compaction flush.
- Eval model only (arcllm); no provider SDK in the memory module.
- Notes writes stay under the append-only / path guards already in `memory_pre_tool`; the two
  consolidation rewrites are owner-driven and guarded by `hook_active`.

## 8. Module-boundary contract (REQ-009)

| Concern | Owner |
|---------|-------|
| Note capture / consolidation / rollup / cadence | `arcagent/modules/memory` |
| Memory config schema | `arcagent/core/config.py` (`MemoryConfig`) |
| Eval model | arcllm (injected via `get_eval_model`) |
| Compaction | `SessionManager` — **no interaction with memory** |

No arcrun, no arcllm, no core-compaction edits.

## 9. Failure modes

- Crash between turns → last raw append survives (Tier 1). Loss bounded to the current in-flight turn.
- Session never ends cleanly → Tier 1 already durable; Tier 2b skipped, next new-day rollup still
  consolidates the raw appends.
- Rollup model failure → fail-open, `prev` stays un-rolled (no marker) → retried next new-day event.
- Config all-off → notes degrade to the current create-file + background-extract behavior (safe).
