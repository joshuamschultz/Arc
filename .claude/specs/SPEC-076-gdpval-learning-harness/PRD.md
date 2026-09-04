# Product Requirements Document: GDPval Learning-Curve Harness

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
Arc can publish GDPval-comparable win-rate stats AND prove that a persistent agent develops procedural memory that makes it measurably better and cheaper on later, similar tasks.

### Problem Statement
Arc has no way to measure task quality against real-world work, and no evidence that its procedural memory (built well during consolidation) actually improves later task performance — because procedures are recalled only via an opt-in tool and never proactively surfaced. Reflexio publishes a warm-vs-warm+memory improvement stat; arc cannot yet produce one.

### Value Proposition
One harness yields two shareable outcomes: (1) a directional win-rate vs human experts, comparable to the public GDPval leaderboard; (2) a learning-curve number showing whether procedural memory pays off — the empirical test of arc's 'gets better over time' claim, and the trigger to close the recall gap.

## Personas

See `.claude/steering/product.md#user-personas`. Primary for this feature: the Secondary Persona (Agent Developer / arc maintainer) who runs evaluations and hardens arcmemory. The Tertiary Persona (Auditor) cares that any external-grader call is classification-aware and audited.

## User Stories

- **US-1**: As an arc maintainer, I want to run the unmodified arc stack against the GDPval gold tasks and score its output against the human deliverable, so that I can publish a directional win-rate comparable to OpenAI's leaderboard..
- **US-2**: As an arc maintainer, I want to run one persistent agent over a family of similar tasks in cold, warm, and warm+memory arms, so that I can measure whether procedural memory makes it better and cheaper on later similar tasks..
- **US-3**: As an arc maintainer, I want procedures to auto-surface at task start and to be learnable from expert deliverables, so that the agent actually improves over time and the learning-curve number goes up..
- **US-4**: As an auditor, I want every deliverable sent to an external grader to be an audited, classification-aware action, so that the harness never leaks classified work to a third-party service..

## Functional Requirements

- **REQ-386** (story US-1, Must): The harness SHALL drive the unmodified arc stack (arcagent, arcmemory, arcrun, arcllm) as a black box, meeting every requirement by configuration or harness-side code and adding zero framework patches to the packages under test.
- **REQ-387** (story US-1, Must): WHEN the harness ingests the GDPval gold corpus THEN it SHALL load tasks from a pinned `openai/gdpval` Hugging Face revision and verify the dataset checksum before making any model call.
- **REQ-388** (story US-1, Must): The harness SHALL turn the corpus into agent input through a GDPval `SourceAdapter` on the shared `evaluations/ingest/` seam, and `evaluations/ingest/` SHALL NOT import the harness or any arc package it drives.
- **REQ-389** (story US-1, Must): WHERE a run is interrupted THEN the harness SHALL resume from a durable per-task ledger and SHALL NOT re-run any task already recorded as scored.
- **REQ-390** (story US-1, Must): The harness SHALL be invocable only as `python -m evaluations.gdpval.cli` and SHALL require an explicit phase and scope argument, never defaulted, before it spends on model or grader calls.
- **REQ-391** (story US-1, Must): WHEN a deliverable is graded THEN the harness SHALL record win-rate and win-or-tie against the human deliverable as two separate figures, each with its denominator and grader identity.
- **REQ-392** (story US-2, Must): WHEN a task completes THEN the harness SHALL record its step count, token count, wall-clock time, and API cost, and SHALL compute the GDPval E[T] and E[C] estimates against the recorded human baselines.
- **REQ-393** (story US-2, Must): WHEN the learning-curve experiment runs over a task family THEN the harness SHALL execute the cold, warm, and warm+memory arms against one persistent agent workspace and SHALL report the warm+memory-minus-warm delta in steps, tokens, and win-rate.
- **REQ-394** (story US-2, Should): WHEN a task runs in the warm+memory arm THEN the harness SHALL record which procedures were surfaced to the agent and which the agent actually used, so exposure is distinguishable from influence.
- **REQ-395** (story US-4, Must): WHERE the configured grader is an external hosted service THEN the harness SHALL treat sending a deliverable as an external-comms action, emit a classification-aware audit record for it, and refuse to send when the run is marked classified.
- **REQ-396** (story US-3, Should): WHEN procedural memory holds a card whose trigger matches the current task AND proactive-procedure-recall is enabled by config THEN arcmemory SHALL surface the matching procedure at task start through proactive recall, without the agent invoking a tool.
- **REQ-397** (story US-3, Could): WHEN a graded task exposes a human expert deliverable THEN arcmemory's distiller SHALL be able to diff the agent output against it and write a trigger, instruction, and pitfall procedure capturing the difference.

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-386, REQ-387, REQ-388, REQ-389, REQ-390, REQ-391, REQ-392, REQ-393, REQ-395 |
| Should | REQ-394, REQ-396 |
| Could | REQ-397 |
| Won't | _(none)_ |

## Success Metrics

Framework: `.claude/steering/product.md#success-metrics-framework` (quality + compliance families). Targets for this feature: (1) a reported win-rate and win-or-tie against the human deliverable on the gold subset, stated beside the public leaderboard anchors (Claude Opus 4.1 ~49% win-or-tie, GPT-5 ~40%); (2) a measurable warm+memory-minus-warm delta in steps and tokens on later similar tasks, reported with sample size and flagged directional under small n; (3) exposure-vs-influence recorded for every surfaced procedure; (4) 100% of external-grader sends carry a classification-aware AuditEvent (quality metric: audit-trail completeness).

## Risks and Constraints

External grader is imperfect (~66% agreement with humans, self-eval bias) and can be unavailable — mitigation: record grader identity per figure, support an expert-grading path later, never present a hosted-grader number as authoritative. Task families are small (gold set is ~5 tasks per occupation) so learning-curve n is low — mitigation: flag every stratum under n=30 as directional, mirror longmemeval's honest-stats posture. Real runs cost money — mitigation: explicit phase/scope gate, dry-run cost estimate, spend ceiling. The measured warm+memory delta may be ~0 because procedures do not auto-surface today — this is the finding the harness exists to produce, not a harness failure; the proactive-recall toggle (REQ-396) lets us measure with and without the fix. Sending classified deliverables to a third-party grader would leak CUI — mitigation: REQ-395 fail-closed refusal.

## Open Questions

- For a defensible (not just directional) win-rate, do we invest in expert pairwise grading, or is the hosted grader sufficient for internal stats?
- How is a 'task family' defined for the learning curve when the gold set has few tasks per occupation — by O*NET occupation, by sector, or by a similarity metric over prompts?
- Do we also run the UK AISI Inspect Evals GDPval harness as an independent cross-check of our numbers?
