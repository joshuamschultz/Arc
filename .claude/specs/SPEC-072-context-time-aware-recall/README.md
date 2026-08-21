# Specification: SPEC-072 Context-Aware and Time-Aware Proactive Recall

**Feature:** `SPEC-072-context-time-aware-recall`
**Created:** 2026-08-20
**Builds on:** [SPEC-071 detected-moment recall](../SPEC-071-detected-moment-recall/) (shipped)

## Status

| Doc | Status | Last Update |
|---|---|---|
| PRD | approved | 2026-08-20 |
| SDD | approved | 2026-08-20 |
| PLAN | approved | 2026-08-20 |
| Implementation | **COMPLETE** (26/26 tasks, RED→GREEN verified) | 2026-08-21 |

## Summary

Extends the shipped detected-moment recall along three axes, all through the one `Brain.on_moment` recall path:

- **A — Working-set recall:** the detectors fire on a bounded per-session set of entities in play, not just the current message's tokens, so recall surfaces cards the literal message would never pull (fixes the "deduped away on user turns" finding from SPEC-071's T-990).
- **B — Mid-loop decision recall:** a decision-point recall reaches the model *between* loop steps by reusing arcrun's existing append-only `transform_context` hook (the seam compaction already uses). arcmemory produces the block via the Brain port; arcagent stages it; arcrun stays unaware, so no dependency arrow reverses.
- **C — Temporal reasoning:** cards carry WHEN they were established, conflicting facts resolve newest-current / older-superseded (mark, never delete), an optional time window filters recall, and the already-stored events stream + daily log answer "what changed."

## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md)
- Tech: [`../../steering/tech.md`](../../steering/tech.md)
- Structure: [`../../steering/structure.md`](../../steering/structure.md)
- Roadmap: [`../../steering/roadmap.md`](../../steering/roadmap.md)

## Scope & Sequencing

14 requirements (REQ-349..362) → 12 components (COMP-001..012) → 26 tasks (T-0992..T-1017), four phases. Capability **B (mid-loop channel)** is the heaviest and is sequenced into Integration, **after** A and C land, so the lower-risk value ships and proves first. Every new path stays deterministic (no LLM/embedder on the trigger/rank path), classification-gated (no-read-up), bounded, audited, degrade-don't-crash, and behind a config toggle — carried from SPEC-071.

## Decision Log Snippets

_(none yet — link decisions as `[D-NNN](../../decisions-log.md#d-nnn)` when they apply.)_

## Phase Notes

### Phase 1: Foundation (T-0992–T-0995) — COMPLETE

Temporal provenance + config toggles landed and verified (RED recovered, GREEN, no regressions, mypy --strict + ruff clean, arch guard green).

- **COMP-006 WHEN-stamp:** `Recall`/`RecallCard` gained `established: str = ""`. Surface cards stamp from `chunks.mtime` → `YYYY-MM-DD` (`surface._established_date`, degrades to `""`); structural (`Insight`) has no date → unstamped. `_to_card` appends the stamp to `provenance`; `security.boundary_mark` adds an `established="…"` attr only when present (no placeholder).
- **Config knobs Phase 2+ depends on:**
  - arcmemory `MemoryConfig`: `working_set_enabled` (True), `working_set_max` (32), `working_set_decay_turns` (5), `temporal_enabled` (True) — all frozen, in every tier.
  - arcagent `modules/memory/config.MemoryConfig`: `working_set_enabled` (True, folded into `backend["dynamics"]`), `decision_point_pre_tool` (False). `_fold_backend_settings` now ALWAYS injects a `dynamics` dict carrying `working_set_enabled`; explicit operator `dynamics` keys win.
- **Provenance note:** Phase 1 was first drafted by two helper agents that overstepped a read-only mapping brief. The drafts were treated as untrusted: reverted to confirm the tests fail feature-absent (RED), restored, then full-suite + type + lint verified before adopting. Both agents were stopped so no background editor races the implementer.

### Phase 2: Core (T-0996–T-1005) — COMPLETE

- **COMP-001 WorkingSet** (`detectors.WorkingSet`): bounded (`working_set_max`), decaying (`working_set_decay_turns`), salience-filtered per-session cue accumulator. `brain.on_moment` updates it and threads it into `_MomentSessionState.working_set`; `_entity_seen` (and `_decision_point`) consult it via `getattr` (backward-compatible with the SPEC-071 `FakeSessionState`). `_augment_query` folds query cues into the proactive search text so a prior-turn entity reaches the text-driven surface channel. Off-switch: `working_set_enabled=False` → empty set → exact SPEC-071 behavior.
- **COMP-002 net-new dedup** (`capabilities._merge_recall`): now dedups per CARD on the `<memory-result source="…">` injection wire-marker (a working-set block sharing a card with query recall no longer double-injects it); source-less blocks fall back to whole-entry dedup (SPEC-071 preserved). arcagent imports no arcmemory type.
- **COMP-007 supersession** (`semantic.superseded_view`): renders newest-current + older-superseded from the existing `was:` trail (mark-not-delete); pure/deterministic.
- **COMP-008 TimeWindow** (`retrieve._within_window` + `window` param on `retrieve`/`recall_cards`): filters by each card's `established`; unstamped kept; `None` = no-op. Reuses the existing `TimeWindow` type.
- **COMP-010 recency tie-break** (`retrieve._rrf_fuse`): recency breaks EXACT score ties only (never overrides a real gap); `_date_ordinal` maps the stamp; unstamped sorts oldest.

### Phase 3: Integration (T-1006–T-1013) — COMPLETE

- **COMP-009 Timeline** (`arcmemory/timeline.py`): `read_timeline(window, clearance)` over `EventStore` + `DailyNotesStore`, chronological, no-read-up gated, degrades to `[]` when stores absent. No new store.
- **COMP-003 mid-loop channel** (`arcagent/core/midloop_recall.py` + `ContextManager.transform_context`): a DID-keyed module-global buffer (survives the sibling-task hazard — the bridge schedules subscribers off-task; a shared dict is visible where a contextvar would not be). `transform_context` drains + appends the block append-only (prefix-stable, ARCRUN_ASSERT_APPEND_ONLY holds). `ContextManager` gained `agent_did` (wired at `agent.py`).
- **COMP-004 emission** (`model_manager.decision_point_moment` + bridge): `turn.start`→pre_plan, `tool.start`→pre_tool (tool+args cues). Emitted unconditionally; the subscriber owns the config gates.
- **COMP-005 routing** (`capabilities.on_agent_moment`): decision_point (gated by `proactive_decision_point`, pre_tool further gated by `decision_point_pre_tool` via the `point` field) routes to the mid-loop buffer; every other kind keeps the assemble buffer.
- **Boundary note:** the whole mid-loop hand-off is arcagent-internal (module→core `midloop_recall`); arcrun stays unaware, arcmemory is never imported by arcrun. Architecture guards green.

### Phase 4: Polish (T-1014–T-1017) — COMPLETE

- **COMP-011 determinism/boundary guard** (`test_determinism_boundary.py`): pins that the trigger/rank helpers take no model/embedder seam and the new modules import no arcagent/arcrun. Passed by construction (no model was ever on the path).
- **T-1016 real-path e2e** (`test_context_time_recall_journey.py`): all three capabilities through a real booted agent (only the LLM wire faked), each falsified by its toggle. Note: proactive recall surfaces the ENTITY CARD (name), not a raw captured-fact marker — assert on the entity name in an empty-query drain to prove net-new.
- **COMP-012 off-switches** (`temporal_enabled` wired in T-1017): `temporal_enabled=False` → surface cards unstamped + recency tie-break off (`_rrf_fuse(recency=…)`), restoring pre-temporal recall. This closed a producers-unwired trap (the field existed but was dead). Degrade paths (no embedder / no events-daily / no timestamp) all covered.

### Verification (final)

- arcmemory: **481 passed**. arcagent: **4098 passed, 17 skipped** (full suite, no regressions).
- `mypy --strict` clean (arcmemory 40 files, arcagent core+memory 43 files); `ruff check` clean both packages; architecture + dependency-boundary tests green.

## Learnings

- **Mid-loop channel already exists:** arcrun exposes `transform_context`, an append-only per-turn message hook (arcagent's compaction uses it). Capability B builds on it — no new arcrun mechanism, arcrun stays independent of memory. This de-risked B substantially (found while designing the SDD).
- Related memory: [[project_spec071_proactive_recall_value_profile]].

## Open Questions

- Mid-loop staging: additive arcrun-facade accessor vs arcagent reading a per-run buffer in its existing `transform_context` callback (confirm against `context.py:492`).
- Working-set membership / bound / decay / salience threshold — tune on the SPEC-060 harness.
- TimeWindow vocabulary: structured (recent/since/period) only, or a no-LLM natural-language time parser.
- decision_point site: pre_plan-only vs pre_tool, decided by measured latency + recall lift.
