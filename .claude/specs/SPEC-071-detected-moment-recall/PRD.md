# Product Requirements Document: Detected-Moment Proactive Recall

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
Memory surfaces the right past context on its own at the moment that calls for it, so the agent recalls like a teammate instead of only answering memory-shaped questions.

### Problem Statement
Today arcmemory recall fires only when the user asks a memory-shaped question, and it runs on the literal message. The analogical/structural engine, arcmemory's one true differentiator, never fires on the live working context. So the agent forgets to remember: it has the relevant card but never pulls it when starting a task, meeting a known entity, or shifting topic.

### Value Proposition
Turns arcmemory from a lookup into proactive memory. Unlocks the analogical engine on real context, raises answer quality without the user prompting, and does it cheaply (deterministic detectors, no LLM on the trigger path) and safely (bounded, classification-gated, audited).

## Personas

See `.claude/steering/product.md#user-personas`. Primary: Federal/Regulated Security Architect (needs bounded, audited, no-read-up recall). Secondary: Agent Developer (Internal) (needs a clean Brain-port seam so arcmemory owns recall logic and arcagent only emits signals).

## User Stories

- **US-1**: As an operator whose agent holds prior context, I want relevant memory surfaced automatically at the right moment, so that the agent recalls what we already established without me asking..
- **US-2**: As an agent developer, I want moment-detection to sit behind a clean Brain-port contract, so that arcagent only emits cheap loop signals and arcmemory owns the decide-and-recall logic..
- **US-3**: As a federal operator, I want proactive recall to stay bounded, classification-gated, and audited, so that it never floods the prompt or leaks memory above my clearance..

## Functional Requirements

- **REQ-340** (story US-2, Must): WHEN the agent loop emits a moment signal through the Brain port THEN arcmemory SHALL decide whether to fire a recall and return a bounded injection, and arcagent SHALL contain no recall-decision logic (Modularity: the split seam keeps memory cadence in arcmemory).
- **REQ-341** (story US-1, Must): WHEN a signal of kind task_start, entity_seen, topic_shift, or decision_point is emitted THEN arcmemory SHALL evaluate the detector registered for that kind (Simplicity: one detector per named moment, no shared branching).
- **REQ-342** (story US-1, Must): The trigger path SHALL reach its fire/no-fire decision using only deterministic signal data (entities in play, task boundary, cue text) and SHALL make no LLM call (Scalability: constant, model-free cost on every turn).
- **REQ-343** (story US-1, Must): WHERE no detector fires for a signal arcmemory SHALL return an empty injection and run no recall query (Simplicity: no cue, no recall — most turns cost nothing).
- **REQ-344** (story US-3, Must): WHEN a detector fires THEN arcmemory SHALL query the analogical/structural engine and return at most a configurable maximum number of cards, default small, so a proactive injection cannot crowd the prompt (Scalability: bounded prompt budget).
- **REQ-345** (story US-3, Must): IF a candidate memory's classification exceeds the caller's clearance THEN arcmemory SHALL exclude it from the injection, applying the same no-read-up gate as explicit recall (Security: proactive path is never a leak bypass).
- **REQ-346** (story US-3, Should): WHILE successive signals in the same turn window reference entities or topics already injected arcmemory SHALL suppress duplicate injections (Scalability: the same card is not re-injected on every signal).
- **REQ-347** (story US-3, Should): WHEN a proactive recall fires THEN arcmemory SHALL emit an audit event recording the trigger kind and the cards surfaced (Security: every proactive surfacing is attributable).
- **REQ-348** (story US-2, Could): WHERE proactive recall is disabled by config the agent SHALL run unchanged and no signal SHALL cause a recall (Simplicity: a single toggle, default set per tier, cleanly removes the capability).

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-340, REQ-341, REQ-342, REQ-343, REQ-344, REQ-345 |
| Should | REQ-346, REQ-347 |
| Could | REQ-348 |
| Won't | _(none)_ |

## Success Metrics

See `.claude/steering/product.md#success-metrics-framework`. Targets: trigger-path adds < 5ms to a turn that fires no detector and < 50ms p95 when one fires (deterministic detectors + bounded query); 0 recall queries on turns with no cue; proactive injection <= configured max cards (default 3); 0 classification leaks on the proactive path (security test); measurable recall lift attributed to proactive triggers, scored via the SPEC-060 LongMemEval harness.

## Risks and Constraints

Prompt crowding from over-firing (mitigation: bounded injection REQ-344 + in-window dedup REQ-346). False triggers on noisy signals (mitigation: deterministic detectors REQ-342, tunable per detector). Boundary bleed pulling cadence logic up into arcagent (mitigation: port contract REQ-340, enforced by the arcmemory no-arcagent-import architecture test). Classification bypass on a new recall path (mitigation: REQ-345 reuses the existing no-read-up gate; security test required). decision_point detection may need a loop hook that does not yet exist in arcagent (carried as an open question).

## Open Questions

- Default values for max injected cards and the dedup turn-window — to be tuned against the SPEC-060 harness, not guessed.
- Does arcagent already expose a hook for decision_point / consequential-action, or must the loop emit a new signal at that point? If absent, decision_point ships as Could, behind the other three detectors.
- Should topic_shift detection reuse the existing entity/tag signal, or does it need a lightweight embedding-distance check on the working context (which would touch the read path but not add an LLM call)?
