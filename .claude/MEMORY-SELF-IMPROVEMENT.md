# Self-improving memory — design

How arcmemory should get better at its own job over time. Grounded in what the
store already records after this branch, and in what the 2026 literature says
actually works.

## What the research converges on

Three findings matter for us, and one of them is a warning.

**Forgetting is the underrated operation.** The framing has moved from preserving
experience to deciding which experience should stay active. Entries that are
wrong, stale, or never relevant accumulate quietly and add noise to *every* future
retrieval — the cost is paid on every turn, not once.

**Outcome-only feedback under-specifies what helped.** Rewarding a whole trajectory
leaves the utility of individual memories unattributed. AttriMem traces credit back
to the specific memory contents that were used, and reports that this beats
outcome-only or action-level reward.

**Per-item learned utility does not survive contact with a real store — this is the
warning.** Giving every stored trajectory its own utility makes the learnable state
grow with the store while feedback stays scarce: most entries never get a signal at
all (utility cold start), updates concentrate on a few, and feedback density
collapses. RoMeRL's fix is to stop learning per item and learn over a
*fixed-dimensional* reduced state instead, reporting an 80% cut in cold entries,
~6× feedback density, and an 84% smaller maintained memory.

That last one rules out the obvious design. A live agent here holds 1,528 indexed
chunks and answers a handful of turns an hour. Per-chunk utility would leave almost
every chunk permanently cold.

## What Arc already records

This branch added most of the signal such a system needs:

| Signal | Where | Meaning |
|---|---|---|
| `use_count` | procedure card | times the playbook was actually reached for |
| `revisions` | procedure card | times it was written or evolved |
| `hits` per step | procedure card | sessions that restated that step |
| fact `confidence` | entity card | corroboration, on both write paths |
| fact currency | derived | confidence discounted by age, never written back |
| `memory.dedup_pass` | audit | entities, candidate clusters, merges per pass |
| `memory.recall` | audit | a recall happened, and whether it hit |

**The one missing channel is outcome.** Nothing records whether a recalled memory
was *useful* — only that it was returned. That is the gap to close first, and it is
also the thing that makes everything below possible.

## Design

### 1. Attribute retrieval, cheaply

Every recall bundle already carries the source id of each item. Record, per turn:
which sources were injected, and whether the turn ended well. "Ended well" is
deliberately coarse — the turn completed without an error and the user did not
immediately restate the same request — because a coarse signal that exists beats a
precise one that does not.

Credit lands on the **card**, not the chunk. A card is the unit an operator edits
and the unit consolidation merges, and there are far fewer of them (326 curated
chunks, far fewer distinct cards) than there are trajectories. This is RoMeRL's
reduced-order move applied to our own shape.

### 2. Let usefulness feed ranking, bounded

A card retrieved often and followed by good turns should rank slightly higher; one
retrieved often and never useful should rank lower. Bounded, because the
`_ensure_curated_present` work on this branch showed how quickly a ranking policy
can suppress genuinely-best results — the guarantee had to be narrowed twice before
it stopped breaking the evidence that each channel was load-bearing.

Start as a tie-breaker, not a multiplier. Measure before widening it.

### 3. Forget by demotion, never by deletion

The operator's rule already stands for facts: an old fact is still history, so
stored confidence never decays and only currency is discounted. Extend the same
shape to whole cards. A card never retrieved and never corroborated for a long
window is *archived* — removed from the recall pool, kept on disk and readable.
Nothing is destroyed; the noise floor drops.

This is the highest-leverage item, because the cost of a stale card is paid on
every retrieval.

### 4. Close the loop on the mechanisms themselves

The de-dup work on this branch is the template. It was invisible, so it was broken
for months; making the pass emit `entities / clusters / merged` identified the true
cause in one line. Every self-improving mechanism must emit what it saw, not only
what it changed — silence has to be impossible.

## Order of work

1. **Attribute recall to cards + record turn outcome.** Nothing else can be
   measured before this exists.
2. **Archive-by-demotion for cold cards.** Highest leverage, lowest risk, matches
   the operator's "old is still history" rule exactly.
3. **Usefulness as a ranking tie-breaker.** Only after (1) has produced enough
   signal to check against.
4. **Per-mechanism audit for each of the above**, on the de-dup pattern.

Deliberately not doing: per-chunk learned utility. The literature says it starves,
and a store this size with this much traffic would starve it faster.

## Sources

- [RoMeRL: Balancing Feedback Coverage and the Memory-Reward Trap in Self-Evolving Agent Memory](https://arxiv.org/abs/2608.02508v1)
- [AttriMem: Attribution-Guided Process Feedback for Agent Memory Learning](https://arxiv.org/abs/2607.21106v1)
- [SelfMem: Self-Optimizing Memory for AI Agents](https://arxiv.org/html/2607.03726v1)
- [When to Forget: A Memory Governance Primitive](https://arxiv.org/pdf/2604.12007)
- [Designing Agentic Memory in 2026](https://thenuancedperspective.substack.com/p/designing-agentic-memory-in-2026)
- [Memory in the Age of AI Agents: A Survey (paper list)](https://github.com/Shichun-Liu/Agent-Memory-Paper-List)
