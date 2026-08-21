# Product Requirements Document: Context-Aware and Time-Aware Proactive Recall

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
Proactive memory recalls on the live working context of the conversation and the decision at hand, and knows when each memory was true, so the agent recalls like a teammate who has been in the room the whole time and remembers what changed.

### Problem Statement
SPEC-071 shipped detected-moment recall but three gaps blunt it. (A) The moment fires on the CURRENT message's words, so query-recall already surfaces the same cards and the proactive block is deduped away — on a live chat turn its only net-new effect is an audit line (verified by SPEC-071's T-990 journey test). (B) The system prompt is assembled once at run start, so a decision-point moment mid-loop (about to plan or use a tool) has nowhere to inject and was gated OFF. (C) Recall has no sense of time: it cannot tell a current fact from a superseded one, cannot answer 'what changed this week', and never stamps WHEN a memory was established — even though the timestamped events stream and daily log are already stored.

### Value Proposition
Turns proactive recall from an audit-only echo into real added context: it surfaces cards the literal message would never pull (working set), it stops a known pitfall right before the agent acts (mid-loop decision recall), and it uses today's truth instead of a stale fact (temporal). All three share the one recall path, so time-awareness enriches every proactively surfaced card for free — and all stay deterministic, bounded, classification-gated, audited, and behind the clean Brain-port seam.

## Personas

See `.claude/steering/product.md#user-personas`. Primary: Federal/Regulated Security Architect (needs bounded, audited, no-read-up recall that a new mid-loop channel does not weaken). Secondary: Agent Developer (Internal) (needs the decide-and-recall logic to stay behind the Brain port so arcmemory owns it and arcagent only emits signals). Builds directly on the shipped SPEC-071 (`.claude/specs/SPEC-071-detected-moment-recall/`).

## User Stories

- **US-1**: As an operator whose conversation has moved on from a topic, I want the agent to recall what we already established even when my latest message does not name it, so that it acts on the full working context, not just the words I just typed..
- **US-2**: As an operator whose agent is mid-task, I want a relevant past decision recalled right before the agent plans or uses a tool, so that a known pitfall stops it in time instead of after the fact..
- **US-3**: As an operator relying on the agent's memory over weeks, I want recalled memory to be time-aware — current versus superseded, and able to answer what changed over a period, so that the agent uses today's truth and can reconstruct how things evolved..
- **US-4**: As an agent developer and federal operator, I want all of this to stay deterministic, bounded, classification-gated, audited, and behind the Brain-port seam, so that more recall power never costs safety or modularity..

## Functional Requirements

- **REQ-349** (story US-1, Must): WHEN a detected moment is evaluated THEN arcmemory SHALL derive its trigger cues from a per-session working set of recently-active entities and topics, not only from the current message's tokens (Simplicity: one working-set source feeds every detector).
- **REQ-350** (story US-1, Must): WHILE a session is active arcmemory SHALL maintain a bounded working set updated from each turn's salient entities and cues, so recall can key off conversation context that is absent from the literal latest message (Scalability: bounded per-session state, no unbounded growth).
- **REQ-351** (story US-1, Must): WHEN proactive recall fires on working-set cues THEN arcmemory SHALL surface only cards that are net-new relative to the same turn's query-driven recall, preserving the existing dedup (Simplicity: the proactive block adds context, never repeats it).
- **REQ-352** (story US-2, Must): WHEN a decision-point moment fires mid-loop THEN a proactive recall SHALL be able to reach the model between loop steps via the per-turn message tier, not only through the system prompt assembled once at run start (Modularity: injection uses the existing per-turn message channel, no new prompt machinery).
- **REQ-353** (story US-2, Must): WHEN the loop reaches a decision point THEN the emitted moment SHALL carry loop-state cues — the tool about to run with its arguments, or the plan step — so recall keys off what the agent is about to do (Simplicity: cues come from the loop state already in hand).
- **REQ-354** (story US-4, Must): The mid-loop injection SHALL preserve the concern boundaries: arcrun owns the loop, arcagent consumes it through the arcrun facade, and arcmemory decides only via the Brain port — arcmemory SHALL NOT import arcagent or arcrun-loop internals (Modularity: enforced by the architecture tests).
- **REQ-355** (story US-2, Should): WHERE decision-point recall is enabled by configuration the loop SHALL inject at the pre-plan point by default, with the pre-tool point available as an explicit opt-in (Scalability: bound the added per-step cost; default to the cheaper site).
- **REQ-356** (story US-3, Must): WHEN recall surfaces a card THEN it SHALL carry when the underlying memory was established, so the agent can judge staleness (Security: provenance includes time, not only source).
- **REQ-357** (story US-3, Must): IF two memories about the same subject conflict THEN recall SHALL present the most recent as current and mark the older as superseded, never deleting the older evidence (Security: the audit trail of what was once true is preserved).
- **REQ-358** (story US-3, Should): WHERE a recall request carries a time window arcmemory SHALL filter or boost results to that window — recent, since a given date, or a named period such as this week (Simplicity: one optional window parameter on the existing recall).
- **REQ-359** (story US-3, Should): WHEN the agent asks what happened or changed over a period THEN arcmemory SHALL read the already-stored timestamped events stream and daily log and return the changes in chronological order (Simplicity: reuse the existing stores as the timeline, add no new store).
- **REQ-360** (story US-3, Could): WHEN two candidate memories score equally on relevance THEN recall SHALL rank the more recent one higher, so recency breaks ties without overriding relevance (Scalability: a cheap deterministic tie-break, no extra query).
- **REQ-361** (story US-4, Must): The trigger and decision path — working-set update, detector evaluation, decision-point cue extraction, and temporal ranking — SHALL make no LLM or embedder call (Scalability: constant, model-free cost on every turn and every loop step).
- **REQ-362** (story US-4, Must): All three capabilities SHALL degrade rather than crash — no embedder falls back to BM25 plus graph, and an absent events stream or daily log skips the temporal features gracefully — while every proactively surfaced card, on any channel, stays bounded, classification-gated by no-read-up, and audited; WHERE a capability is disabled by configuration the agent SHALL run exactly as before (Security and Simplicity: safety invariants hold on every new path, and each capability has a clean off switch).

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-349, REQ-350, REQ-351, REQ-352, REQ-353, REQ-354, REQ-356, REQ-357, REQ-361, REQ-362 |
| Should | REQ-355, REQ-358, REQ-359 |
| Could | REQ-360 |
| Won't | _(none)_ |

## Success Metrics

See `.claude/steering/product.md#success-metrics-framework`. Targets: on a live chat turn where the working set holds a relevant entity absent from the message, proactive recall surfaces at least one net-new card that query-recall does not (measured on the SPEC-060 LongMemEval harness); working-set update + detector + temporal-rank adds < 5ms to a turn that fires nothing and < 50ms p95 when a recall fires; a mid-loop decision recall reaches the model before the tool call in a real run (real-path e2e, not a mock); 0 classification leaks on the mid-loop channel (security test); when a fact is superseded, recall returns the current value and marks the old one in 100% of conflict cases; 0 new store engines introduced.

## Risks and Constraints

Working set over-broadens and recall fires too often (mitigation: bounded set of salient entities only REQ-350, net-new dedup REQ-351, existing card bound). The mid-loop channel is a real loop change that could reorder or bloat turns or leak classification (mitigation: reuse the existing per-turn message tier REQ-352, keep arcrun/arcagent/arcmemory boundaries REQ-354, run the same gate+bound+audit as start-of-run REQ-362, security test required). Temporal supersession could hide evidence (mitigation: mark-not-delete REQ-357, old confidence retained as it is today). Scope: three capabilities in one spec — B (mid-loop channel) is the heaviest and the riskiest and MAY be phased last in the PLAN so A and C can land first. Temporal correctness depends on trustworthy timestamps in the events/daily stores (assumed present per SPEC-041).

## Open Questions

- Working set membership: which entities count as in play, and how many turns / what decay keeps it bounded — tune against the SPEC-060 harness, not guessed.
- Mid-loop channel shape: does injecting into the per-turn message tier at pre_plan require an arcrun-facade addition, or can arcagent write the turn tier without touching arcrun internals? Resolve in the SDD against the SPEC-071 T-990 finding about wire_messages / the turn tier.
- Temporal window vocabulary: is a structured window (since-date / period enum) enough, or is a light natural-language time parser needed on the recall query (no LLM)?
- Supersession detection: same-subject conflict is clear for a single entity fact; how far to take it for insights/procedures is deferred unless cheap.
