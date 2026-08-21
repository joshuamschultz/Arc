# Solution Design Document: Context-Aware and Time-Aware Proactive Recall

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

Extends the shipped SPEC-071 detected-moment recall along three axes that all flow through the one Brain.on_moment recall path, so none is a new subsystem. (A) The detectors fire on a per-session WORKING SET that arcmemory accumulates inside on_moment from the primitive cues each moment already carries, so recall keys off entities in play across the conversation, not just the latest message's tokens. (B) A decision-point recall reaches the model MID-LOOP by re-using arcrun's existing append-only `transform_context` hook (the same seam compaction uses) — arcmemory produces the block via the Brain port, arcagent stages it into the hook, arcrun runs unaware, so no dependency arrow reverses. (C) The retriever becomes TIME-AWARE — cards carry an establishment timestamp, conflicting facts resolve newest-current / older-superseded, an optional time window filters/boosts, and the already-stored events stream + daily log answer 'what changed'. Markdown stays source-of-truth, SQLite stays the disposable index, no new store engine. See `.claude/steering/structure.md#module-boundaries` and `.claude/steering/tech.md`.

## Architecture

Flow A (working set): agent:moment emit (unchanged, arcagent) -> memory subscriber -> brain.on_moment(kind, cues, text, session_id) -> arcmemory MERGES the incoming cues/entities into a bounded per-session WorkingSet (generalizes the SPEC-071 per-session prior_cues) -> detectors evaluate against the working set -> gated/bounded/deduped recall as today -> same-turn assemble-prompt injection. Flow B (mid-loop): loop reaches a decision point -> arcagent emits agent:moment kind=decision_point with loop-state cues (tool+args at agent:pre_tool, plan step at agent:pre_plan) -> memory subscriber calls brain.on_moment -> when it returns text for a decision_point, the subscriber stages it on a per-run injection buffer that the agent's `transform_context` callback (arcagent core/session_internal/context.py:492, already passed into arcrun.run_stream at agent_dispatch.py) APPENDS to the message tail before the next model call (append-only contract, composes with compaction) -> arcrun sends it, unaware it is memory. Flow C (temporal): Retriever/stores read the establishment timestamp already on episodic/semantic records; a TemporalRanker applies recency tie-break + supersession marking; recall accepts an optional TimeWindow that filters candidates; a Timeline reader walks the events store + daily log for 'what changed'. All three keep the arcmemory no-arcagent-import boundary and the deterministic (no-LLM/embedder) trigger/rank path. Error-handling + degrade per `.claude/steering/tech.md#error-handling-pattern`.

## Components

### COMP-001: Session WorkingSet (arcmemory)
**Responsibility:** A bounded, per-session accumulator of salient entities/cues in play, updated inside on_moment from each moment's primitive cues (generalizes SPEC-071's per-session prior_cues). Detectors evaluate against the working set, so recall keys off conversation context absent from the latest message. Salience filter keeps proper-noun / known-entity cues over generic tokens; bounded size + decay by turn so it never grows unbounded.
**Dependencies:** A, r, c, M, e, m, o, r, y, B, r, a, i, n,  , (, b, r, a, i, n, ., p, y, ), ,,  , d, e, t, e, c, t, o, r, s, ., e, v, a, l, u, a, t, e, _, m, o, m, e, n, t, ,,  , S, e, m, a, n, t, i, c, S, t, o, r, e,  , (, k, n, o, w, n, -, e, n, t, i, t, y,  , c, h, e, c, k, )
**Inputs:** (session_id, incoming_cues: list[str], text)
**Outputs:** the merged working-set cue list fed to the detectors; internal per-session state bounded to a configured max. No LLM/embedder call.

### COMP-002: Net-new injection dedup (arcagent memory module)
**Responsibility:** Preserve the SPEC-071 behavior that a proactive block is merged into sections['recall'] deduped against the query-driven recall, now for working-set-sourced cards: only cards the literal-message query recall did not surface are added. Reuses the existing inject_recall _merge_recall dedup; no new machinery.
**Dependencies:** C, O, M, P, -, 0, 0, 1, ,,  , e, x, i, s, t, i, n, g,  , i, n, j, e, c, t, _, r, e, c, a, l, l, /, _, m, e, r, g, e, _, r, e, c, a, l, l,  , (, m, o, d, u, l, e, s, /, m, e, m, o, r, y, /, c, a, p, a, b, i, l, i, t, i, e, s, ., p, y, )
**Inputs:** proactive buffer text + query recall text
**Outputs:** merged sections['recall'] with duplicates suppressed.

### COMP-003: Mid-loop injection channel (arcagent over arcrun transform_context)
**Responsibility:** Deliver a decision-point recall to the model BETWEEN loop steps by appending it to the message tail via arcrun's existing append-only `transform_context` hook — NOT a new arcrun mechanism and NOT the fixed start-of-run system prompt. arcmemory produces the block via the Brain port; arcagent stages it on a per-run buffer and its transform_context callback appends it (composing with compaction, honoring the append-only/prefix-stable contract). arcrun stays unaware of memory (dependency arrows preserved).
**Dependencies:** a, r, c, r, u, n,  , R, u, n, S, t, a, t, e, ., t, r, a, n, s, f, o, r, m, _, c, o, n, t, e, x, t,  , (, a, p, p, e, n, d, -, o, n, l, y,  , p, e, r, -, t, u, r, n,  , h, o, o, k, ), ,,  , a, r, c, a, g, e, n, t,  , S, e, s, s, i, o, n, C, o, n, t, e, x, t, ., t, r, a, n, s, f, o, r, m, _, c, o, n, t, e, x, t,  , (, c, o, n, t, e, x, t, ., p, y, :, 4, 9, 2, ), ,,  , C, O, M, P, -, 0, 0, 5, ,,  , b, o, u, n, d, a, r, y, _, m, a, r, k, /, r, e, n, d, e, r,  , (, a, r, c, m, e, m, o, r, y,  , r, e, u, s, e, d,  , t, e, x, t, )
**Inputs:** a staged proactive recall block for the current run + the live message list
**Outputs:** the message list with the block appended to the tail (prefix unchanged). Empty when nothing staged; append-only assertion must hold (ARCRUN_ASSERT_APPEND_ONLY).

### COMP-004: Decision-point moment emission (arcagent loop)
**Responsibility:** Emit agent:moment kind=decision_point carrying loop-state cues at the decision points: the plan step at agent:pre_plan (default) and the tool name+args at agent:pre_tool (opt-in). Thin, best-effort emits on the existing bridge/hook points; net-new instrumentation only.
**Dependencies:** M, o, d, u, l, e, B, u, s, ,,  , a, g, e, n, t, :, p, r, e, _, p, l, a, n, /, a, g, e, n, t, :, p, r, e, _, t, o, o, l,  , (, c, o, r, e, /, m, o, d, e, l, _, m, a, n, a, g, e, r, ., p, y,  , b, r, i, d, g, e, ,,  , t, o, o, l, _, r, e, g, i, s, t, r, y,  , p, r, e, _, t, o, o, l, ), ,,  , m, o, m, e, n, t, _, c, u, e, s,  , (, u, t, i, l, s, /, m, o, m, e, n, t, ., p, y, )
**Inputs:** loop reaching pre_plan/pre_tool
**Outputs:** bus event agent:moment {kind:'decision_point', cues, text, session_id}. No behavior change when unsubscribed.

### COMP-005: Decision-point recall routing (arcagent memory module)
**Responsibility:** The agent:moment subscriber, for kind=decision_point (behind the SPEC-071 proactive_decision_point config, now honored), calls brain.on_moment and routes a non-empty result to the mid-loop buffer (COMP-003) instead of the assemble-prompt buffer, keeping clearance='unclassified' and the getattr optional-method pattern. pre_plan default; pre_tool opt-in via config.
**Dependencies:** C, O, M, P, -, 0, 0, 3, ,,  , C, O, M, P, -, 0, 0, 4, ,,  , A, r, c, M, e, m, o, r, y, B, r, a, i, n, ., o, n, _, m, o, m, e, n, t, ,,  , M, e, m, o, r, y, C, o, n, f, i, g,  , (, p, r, o, a, c, t, i, v, e, _, d, e, c, i, s, i, o, n, _, p, o, i, n, t, ,,  , n, e, w,  , p, r, e, _, t, o, o, l,  , f, l, a, g, )
**Inputs:** EventContext for a decision_point moment
**Outputs:** mid-loop buffer updated; nothing when disabled or empty.

### COMP-006: Temporal provenance / WHEN-stamp (arcmemory)
**Responsibility:** Every surfaced card carries when its underlying memory was established, read from the timestamp already stored on episodic/semantic/event records. Rendered into the recall card's provenance so the model can judge staleness. No new field on disk if a timestamp already exists; surfaced through RecallCard/Recall + render.
**Dependencies:** R, e, c, a, l, l, C, a, r, d, /, R, e, c, a, l, l,  , (, t, y, p, e, s, ., p, y, ), ,,  , s, t, o, r, e, s,  , (, e, p, i, s, o, d, i, c, /, s, e, m, a, n, t, i, c, /, e, v, e, n, t, s, ), ,,  , r, e, n, d, e, r, _, r, e, c, a, l, l, s,  , (, s, e, c, u, r, i, t, y, ., p, y, )
**Inputs:** a gated recall's source records
**Outputs:** each card annotated with an establishment timestamp; absent-timestamp degrades to unstamped, never errors.

### COMP-007: Supersession resolver (arcmemory)
**Responsibility:** When two memories about the same subject conflict, present the most recent as current and MARK the older as superseded — never delete it (the old value/confidence is retained as evidence, consistent with the existing fact-currency model). Deterministic on timestamps; no LLM.
**Dependencies:** S, e, m, a, n, t, i, c, S, t, o, r, e,  , f, a, c, t, s,  , +,  , f, a, c, t, _, h, a, l, f, _, l, i, f, e, _, d, a, y, s,  , c, u, r, r, e, n, c, y,  , (, c, o, n, f, i, g, ., p, y, ), ,,  , C, O, M, P, -, 0, 0, 6
**Inputs:** same-subject candidate memories with timestamps
**Outputs:** current value surfaced + older marked superseded in the card; ordering deterministic; old evidence preserved on disk.

### COMP-008: Temporal window filter (arcmemory retrieve)
**Responsibility:** Recall accepts an optional TimeWindow (recent / since a date / a named period) and filters or boosts candidates to that window before the bound. One optional parameter threaded through retrieve/recall; absent window = today's behavior unchanged.
**Dependencies:** R, e, t, r, i, e, v, e, r,  , (, r, e, t, r, i, e, v, e, ., p, y, ), ,,  , S, i, t, u, a, t, i, o, n, /, t, y, p, e, s, ,,  , C, O, M, P, -, 0, 0, 6
**Inputs:** (situation, optional TimeWindow)
**Outputs:** candidate set filtered/boosted to the window; None window is a no-op.

### COMP-009: Timeline reader (arcmemory) — what changed
**Responsibility:** Answer 'what happened/changed over a period' by reading the ALREADY-STORED timestamped events stream and daily log for the window and returning the changes in chronological order. Reuses existing stores as the timeline; adds no store.
**Dependencies:** s, t, o, r, e, s, /, e, v, e, n, t, s, ., p, y, ,,  , s, t, o, r, e, s, /, d, a, i, l, y, ., p, y, ,,  , C, O, M, P, -, 0, 0, 8
**Inputs:** a time window
**Outputs:** ordered list of dated changes/summaries; empty (not error) when no events/daily entries exist for the window.

### COMP-010: Recency ranking tie-break (arcmemory fusion/retrieve)
**Responsibility:** When two candidates score equally on relevance, rank the more recent higher — a cheap deterministic tie-break that never overrides a real relevance gap. Applied in the fuse/rank step before the bound.
**Dependencies:** f, u, s, i, o, n, ., p, y,  , /,  , _, r, r, f, _, f, u, s, e,  , (, r, e, t, r, i, e, v, e, ., p, y, ), ,,  , C, O, M, P, -, 0, 0, 6
**Inputs:** ranked candidates with timestamps
**Outputs:** stable order with recency as the tie-break key only.

### COMP-011: Determinism + boundary guard
**Responsibility:** The whole trigger/decision/rank path (working-set update, detectors, decision-point cue extraction, temporal ranking + supersession) makes NO LLM/embedder call, and arcmemory imports neither arcagent nor arcrun-loop internals — the mid-loop block is produced as primitives and consumed by arcagent. Enforced by the existing arcmemory architecture tests plus an assertion the new code paths take no model/embedder seam.
**Dependencies:** t, e, s, t, s, /, a, r, c, h, i, t, e, c, t, u, r, e, /, t, e, s, t, _, n, o, _, a, r, c, a, g, e, n, t, _, i, m, p, o, r, t, ., p, y, ,,  , t, e, s, t, _, d, e, p, e, n, d, e, n, c, y, _, b, o, u, n, d, a, r, i, e, s, ., p, y
**Inputs:** static import graph + the new code paths
**Outputs:** architecture tests green; no model/embedder call reachable on the trigger/rank path.

### COMP-012: Degrade + safety invariants + config
**Responsibility:** Every new path degrades not crashes (no embedder -> BM25+graph; no events/daily -> temporal features skip; no timestamp -> unstamped card) and stays bounded, classification-gated (no-read-up) and audited exactly as start-of-run recall — including the mid-loop channel (same gate+bound+audit before a block is staged). Config toggles: working-set on/off, proactive_decision_point (+ pre_tool opt-in), temporal on/off; each disabled toggle returns the agent to prior behavior.
**Dependencies:** s, e, c, u, r, i, t, y, ., g, a, t, e, _, n, o, _, r, e, a, d, _, u, p,  , +,  , e, n, f, o, r, c, e, _, b, u, d, g, e, t, ,,  , b, r, a, i, n, ., _, e, m, i, t, _, r, e, c, a, l, l, _, a, t, t, r, i, b, u, t, i, o, n, ,,  , M, e, m, o, r, y, C, o, n, f, i, g,  , (, a, r, c, m, e, m, o, r, y, ),  , +,  , M, e, m, o, r, y, C, o, n, f, i, g,  , (, a, r, c, a, g, e, n, t,  , m, o, d, u, l, e, )
**Inputs:** any new path under missing-dependency or disabled-config conditions
**Outputs:** graceful degrade or exact prior behavior; every mid-loop surfaced card is gated+bounded+audited with trigger=decision_point.


## Data Model

No new persistent store and no new store engine (markdown source-of-truth + SQLite disposable index unchanged). COMP-006/007 READ establishment timestamps already stored on episodic/semantic/event records; supersession MARKS (does not delete) — the older value + confidence stay on disk as today. WorkingSet (COMP-001) and the mid-loop injection buffer (COMP-003/005) are in-memory, session/run-scoped, bounded, rebuild-free. TimeWindow (COMP-008) is a small typed value (kind: recent|since|period, bounds) passed through recall; the cross-seam signal stays primitives (the decision-point cue/text remain str/list). Temporal ordering derives from existing timestamps; no schema migration.

## External Integrations

None new. Reuses arctrust classification (dominates/parse_classification) + audit already wired into arcmemory, and arcrun's existing append-only transform_context hook (no new arcrun API surface; if a thin arcrun-facade accessor is needed it is additive and one-directional). No FalkorDB/Graphiti/Redis, no new network dependency.

## Traceability

| Requirement | Components |
|---|---|
| REQ-349 | COMP-001 |
| REQ-350 | COMP-001 |
| REQ-351 | COMP-002 |
| REQ-352 | COMP-003, COMP-005 |
| REQ-353 | COMP-004 |
| REQ-354 | COMP-003, COMP-011 |
| REQ-355 | COMP-004, COMP-005 |
| REQ-356 | COMP-006 |
| REQ-357 | COMP-007 |
| REQ-358 | COMP-008 |
| REQ-359 | COMP-009 |
| REQ-360 | COMP-010 |
| REQ-361 | COMP-011 |
| REQ-362 | COMP-012 |

## Alternatives Considered

A1: Build a NEW mid-loop injection mechanism in arcrun (REJECTED: arcrun already exposes the append-only transform_context hook that compaction uses; a new mechanism would duplicate it and risk the provider-cache prefix invariant). A2: Maintain the working set on the arcagent side and pass it in cues (REJECTED: working-set cadence is memory logic and belongs in arcmemory behind the Brain port — arcagent stays a socket, per the arcagent-is-a-memory-socket boundary; on_moment already receives the primitives it needs). A3: Use the parked SPEC-059 steer_queue/Injection channel for mid-loop injection (REJECTED for MVP: it is unimplemented/parked; transform_context is live and proven by compaction — revisit if a true live mid-turn steer is later needed). A4: A dedicated temporal graph store / bi-temporal DB (Graphiti/FalkorDB) (REJECTED: out of scope per the chosen substrate; the timestamped events stream + daily log already provide the timeline, and supersession is a deterministic mark on existing records). A5: An LLM time parser for windows (REJECTED for MVP: a structured TimeWindow (recent/since/period) keeps the trigger path model-free per REQ-361; a light no-LLM parser is a possible follow-on).

## Risks and Mitigations

Mid-loop transform_context append that violates the append-only/prefix-stable contract would break the provider cache or corrupt history (mitigation: append to the tail only, compose after compaction, run with ARCRUN_ASSERT_APPEND_ONLY in tests — COMP-003). Working set over-broadening -> over-firing (mitigation: salience filter + bound + net-new dedup — COMP-001/002). Supersession hiding evidence (mitigation: mark-not-delete, old record retained — COMP-007). Decision-point emit on every pre_tool adds per-step latency on tool-heavy runs (mitigation: pre_plan default, pre_tool opt-in — COMP-004/005). Classification leak on the new mid-loop channel (mitigation: same gate+bound+audit before staging — COMP-012, security test required). Scope: capability B (COMP-003/004/005) is the heaviest and is sequenced LAST in the PLAN so A (COMP-001/002) and C (COMP-006..010) land and prove value first. Temporal correctness assumes trustworthy stored timestamps (SPEC-041).

## Open Questions

- COMP-003: does staging the mid-loop block need a thin additive arcrun-facade accessor, or can arcagent's existing transform_context callback read a per-run buffer directly? Confirm against context.py:492 + agent_dispatch transform wiring during implementation; keep any arcrun addition additive and one-directional.
- COMP-001: exact working-set membership + bound + decay (how many turns, salience threshold) — tune on the SPEC-060 harness, not guessed.
- COMP-008: TimeWindow vocabulary — structured (recent/since/period) only, or add a no-LLM natural-language parser on the recall query?
- COMP-007: supersession beyond single-entity facts (insights/procedures) — deferred unless cheap.
- COMP-004: is decision_point best at pre_plan (coarser, cheaper) or does pre_tool materially lift recall — decide by measured latency + recall lift, mirroring SPEC-071 T-991.
