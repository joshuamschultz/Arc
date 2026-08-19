# SPEC-070 — Recursive reading strategy (process everything, combine into a full view)

| | |
|---|---|
| **Status** | **DRAFT — raised, not scheduled.** Requested by Josh 2026-08-18 after a workflow node blew its cost cap on one oversized read. |
| **Type** | capability / arcrun strategy |
| **Package(s)** | `arcrun` (strategy), `arcagent` (read/tool path), `arcui` (trace visibility) |
| **Relates to** | arcrun strategies (D-589..594, "RLM recursive reading" — designed, never built); the observation-cap dead end (rejected: capping loses data); SPEC-043 budget breaker |

## The finding

A workflow node (sales_agent ingesting Dropbox meeting transcripts) read a
multi-MB file with the default `read` (`limit=0`), which returned the whole file
as a single ~589,000-token observation. That one model call cost ~$1.77, so two
calls blew the per-run cost cap ($2, since raised to $5) and the run finished
**incomplete** — "Cost limit reached before task completed."

The reflexive fix — cap/truncate the observation — is **wrong and rejected**:
truncating throws away content the agent needs. "We can't just cap context,
we'll lose a ton of what we need." A read that drops 90% of a transcript cannot
extract the action items that were the whole point.

Recursive reading — designed in D-589..594 — was expected to already exist and
handle exactly this. It does not. A repo-wide search finds no RLM / recursive
strategy; the arcrun registry loads only `react`, `code`, `dynamic`, `oneshot`,
`plan_execute`. This is the producers-unwired pattern again: designed, never
built. Per Josh: **build, wire, and use — or remove.** Here: build.

## What to build (Josh's design, verbatim intent)

> "We need a recursive strategy that goes through everything and then combines
> the answer later for a full view (with whatever the system prompt that is fed
> to all the children to use to look for and do)."

A first-class **recursive-reading strategy** in `arcrun` — a map-reduce over
large content that loses nothing:

1. **Split** the oversized content into bounded chunks (each safely under the
   context/cost ceiling — no single call is ever the 589k-token blob).
2. **Map (children):** run a child over **every** chunk. Every child is fed the
   **same system prompt** — the instruction for what to look for and what to do
   with it — plus its one chunk. Children run in parallel where safe.
3. **Reduce (combine):** synthesize the children's partial answers into one
   full-view result the parent run continues from. Nothing is dropped; the full
   document is processed, just never in a single call.

### Requirements (to be firmed up in PRD/SDD)

- **R-1 Completeness over truncation.** Every chunk is processed. The
  anti-goal is data loss. A cap, if any survives, is a pure last-resort backstop
  set far above normal operation — never the mechanism.
- **R-2 Auto-engaged, deterministically.** Large-content work routes into the
  recursive strategy without an extra per-turn `select_strategy` LLM call (see
  "Strategy-select gate was the default route" — un-pinned turns must not pay a
  routing call). The trigger is a deterministic size threshold on the read/tool
  result, not model discretion.
- **R-3 Shared child instruction.** One system prompt is fed to all children;
  it carries what to look for and what to do. Where it comes from (the node's
  task, the parent's goal, or an explicit strategy arg) is a design question for
  the PRD.
- **R-4 Combine into a full view.** The reduce step produces one coherent
  result for the parent, not a pile of fragments.
- **R-5 Bounded & accountable.** Chunk count, per-child budget, and total budget
  are capped; every child is a normal accountable run (identity, sign,
  authorize, audit) so the recursion cannot become an unbounded fan-out. Ties
  into the SPEC-043 breaker and per-run cost ceilings.
- **R-6 Visible in the trace.** The run trace must show the strategy was chosen
  and show the map/reduce structure (children + combine), consistent with the
  "Chose the '<name>' strategy" trace work already shipped.

## Open design questions for the PRD

- Strategy vs. tool: a `recursive_read` **strategy** (first-class run mode, Josh's
  ask) vs. a `read`-path capability. Josh chose the strategy.
- Where the shared child instruction is sourced (R-3).
- How children combine (single reduce call vs. hierarchical reduce for very
  large N — true RLM recursion).
- Whether children are full ArcRun sub-agents (spawn) or a lighter in-loop map.
- Interaction with prune/compact: recursive reading should mean the parent never
  needs to hold raw chunks, so context stays small by construction.

## Interim state (until this ships)

- The per-run cost cap is raised to $5 on sales_agent (stopgap, not a fix).
- No truncation/observation cap was merged — capping was rejected. Until this
  spec lands, a large read still costs what it costs and can hit the budget; the
  run now at least fails **honestly** (SPEC status-honesty fix, commit 006d57e9)
  instead of reporting a clean "done."

## Next step

Run `/specify SPEC-070` (or `/build`) to take this DRAFT through PRD → SDD →
PLAN. Do not implement ad hoc — this is deliberately its own spec.
