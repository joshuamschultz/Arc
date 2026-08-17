# ADR-032: An agent is chosen for a channel message by routing, not by self-assessment

**Status**: Accepted
**Date**: 2026-08-17
**Relates to**: SPEC-068 (human + agent group chat), `arcagent.modules.messaging.activation`
**Supersedes**: the per-agent, per-message relevance gate shipped in `75f56ed3`

## Context

Six agents and one operator share a channel. The operator asked *"who has the technical
requirements for NNL?"* without naming anyone. He knew one agent held the answer. Every agent
answered no.

The shipped design gave each agent its own bounded yes/no LLM gate: *is this message relevant to
you?* The gate ran with **no tools, no memory access, and no retrieval**. So the agent that held
the NNL document had nothing to consult, and answered from nothing.

That is the whole defect, and it is not a tuning problem. *"Do I have the NNL technical
requirements?"* is a retrieval query wearing a yes/no costume. A relevance judgement made without a
lookup is a coin flip, and it stays a coin flip at any temperature, with any prompt, on any model.

Two further problems came with it.

**The shape is unique to us.** A survey of shipped systems found no production system that runs a
per-agent, per-message relevance gate. Claude Tag is mention-gated, with untagged traffic swept
later in one full session. Slack's own agent surface does nothing at all without an `@`. AutoGen,
LangGraph, CrewAI, the OpenAI Agents SDK and A2A all make **one decision with every candidate
visible at once** — `speaker_selection_method="auto"`, a supervisor node, a manager LLM, handoff
descriptions, Agent Cards. Four independent teams converged on routing over a short declared
capability string rather than over an agent's internal state.

**The economics are inverted.** Our design is O(N) model calls where each call sees exactly one
candidate and cannot compare it against the others. A single router is O(1) and can. At 500
messages a day with six agents that is roughly $135/month for wrong answers, against roughly
$5/month with prompt caching for right ones. We paid six times more for a strictly worse decision,
and spent it in the worst possible place — a routing decision, which is the cheapest thing in the
system to get right and the most expensive thing to get wrong.

Self-selection is also measured in the literature, and it fails in both directions at once. The
Murder Mystery Agents study compared self-selection, round-robin, and current-speaker-nominates:
self-selection produced monopolisation when scores ran high and silence when they ran low, with
dialogue breakdowns per ten turns of 1–8 against ~1 for nomination (χ²=42.171, p<0.001). Those are
precisely the two failure modes this channel exhibited.

## Decision

**Responder selection is a routing decision made once per message with all candidates visible, over
indexes agents publish, and it never resolves to silence.**

Four parts, in the order a message meets them.

**1. Explicit address resolves deterministically, before anything else runs.** A mention or a reply
to a specific agent is answered by that agent, with no scoring and no model call. This is the
cheapest correct decision available and it must never be reachable only after a probabilistic gate.

**2. Otherwise a hybrid prefilter over published memory indexes selects the top one or two.** Each
agent maintains a small **public digest** of what its private memory holds — entity names,
document titles, project tags, one line per artifact. Not contents. Written at ingest time, so
query time is cheap; this is the same trade Slack makes by indexing every message within
milliseconds of delivery so that search is fast later.

The router runs BM25 and embedding retrieval over the six digests and fuses them with Reciprocal
Rank Fusion. **BM25 is not optional.** `NNL` is a rare proper-noun token, and dense embeddings are
weakest on exactly that — rare acronyms and identifiers get smeared into a semantic neighbourhood,
while BM25 anchors on the exact token. For the question that exposed this defect, the lexical half
of the prefilter is the half that finds the answer, and it costs no model call at all.

**3. A single router call breaks ties when the prefilter is ambiguous.** One prompt, all candidate
cards, one decision — the shape every framework converged on.

**4. A named default responder catches everything else. Silence is never the fallback.** When
nothing scores above threshold, one designated agent answers — including to say that nobody here
owns this and offer to go looking. An unanswered question in a channel is indistinguishable from a
broken system, which is how this defect stayed invisible for four days.

Two structural rules apply throughout:

- **Other agents' messages are filtered out of an agent's context.** An agent that cannot see
  another agent's reply cannot reply to it. This ends pile-on and acknowledgement loops by
  construction rather than by budget, and it is what Claude Tag does — its 50-message thread
  context has other bots' replies filtered out.
- **Only human-authored posts enter the fan-out at all** (SPEC-068), so an agent's reply cannot
  wake another agent.

## Why routing over published indexes rather than the alternatives

**Not capability cards alone.** Routing on a declared capability string is what the frameworks do,
and it is cheap and comparable. But it answers *what an agent was configured to be good at*, not
*what it actually holds today*. If nobody declared "I own NNL documents," a card-only router still
misses the message that started this. Cards are the tiebreak, not the primary signal.

**Not a live search of every agent's private memory.** This is the most *correct* option — it
touches real memory rather than a proxy — and it is affordable only while the per-agent gate stays
a pure local lexical/vector lookup with no model call. The moment it needs a model it collapses
back into the cost profile we are leaving. It remains a legitimate future refinement; it is not the
first thing to build.

**Not mention-only with a deferred sweep**, which is what Claude Tag ships and is a genuinely good
answer. It costs latency measured in minutes for an unaddressed question, and it does not let the
operator ask a room a question and get an answer now. We adopt the sweep as a **backstop** for what
the fast path misses, not as the fast path.

**The published-index design is the only one that satisfies our actual constraint.** Each agent's
memory is private and invisible to the others; Claude Tag sidestepped this problem by having one
agent with workspace-wide shared memory, which is a materially easier problem than ours. A digest
is the only artifact that crosses the privacy boundary, each agent decides what it publishes, and
the agent holding the NNL requirements becomes findable **because it published a pointer when it
filed the document** — not because it guessed correctly about itself.

## Consequences

Selection cost falls from six model calls per message to one embedding, a BM25 pass, and a fusion —
with a single router call only when the prefilter is ambiguous. Marginal cost lands under a cent per
message at six agents, and the answer improves.

Agents must now maintain a published digest, which is new work at ingest time and a new artifact to
keep honest. A stale digest degrades routing silently, so digest freshness needs to be observable.

Routing quality becomes measurable in a way self-assessment never was: the prefilter's ranking can
be inspected, replayed, and regression-tested against real questions. A wrong route leaves
evidence.

**The bounded one-shot model call becomes a named strategy in `arcrun`.** The gate this ADR removes
reached a provider handle directly from `arcagent` (`ArcAgent.quick_classify` → `_ensure_model()` →
`model.invoke()`), which bypasses the loop `arcrun` owns and violates concern purity. The
architecture test did not catch it because it inspects import statements, and the bypass travelled
through an object handle. Cheap inference is a legitimate need and gets a first-class home rather
than a side door — and the boundary test must be extended to catch a provider reached by handle,
not merely one reached by import.

## References

- SPEC-068 — human + agent group chat
- Murder Mystery Agents, arXiv 2412.04937 — self-selection produces monopolisation and silence
- RAG-MCP, arXiv 2505.03275 — retrieval over tool descriptions; selection accuracy 13.62% → 43.13%
- AutoGen `speaker_selection_method`; LangGraph supervisor; A2A Agent Card `skills[]`
- Claude Tag — mention-gated entry, scheduled sweeps, other bots filtered from thread context
