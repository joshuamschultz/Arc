---
name: competitive-read
description: "Synthesize accumulated market and competitor signals into what actually changed, which placed bets it presses on, and what it would take to be wrong. TRIGGER: 'what are competitors doing', 'what changed in the market', or a batch of signals worth reading together. SKIP: deciding a specific pending choice (use decision-brief) or auditing internal beliefs against evidence (use assumption-audit)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the read must clear before it is delivered.
- `examples/competitor-pricing-shift.md` — a worked read separating a real move from noise.

## Contract
Given the signals in `workspace/strategy/signals.md` and any new input, produce a read such that:
1. Signals are grouped into **moves** (a competitor did something) and **conditions** (the
   market changed), because they demand different responses.
2. Each move states the actor, what they did, and the date — never a vague "competitors are".
3. Every claim carries a source; a signal with no source is reported as **unsourced**, not dropped.
4. The read names which placed bets each change presses on, by thesis slug.
5. Noise is explicitly separated from signal, with the reason it is noise.
6. It ends with **what would change our mind** — the observable that would force a response.
New signals encountered are written back with `strategy_log_signal`.

## Knowledge
- A competitor announcement is not a competitor capability. Separate what was shipped from
  what was said; the gap between them is often the most useful fact available.
- One move is an event. The same move by three actors is a market condition, and it is a
  different thing to respond to.
- The response to a rival's move is usually "nothing", and saying so plainly is more valuable
  than manufacturing a reaction. Reacting to every move is how strategy becomes drift.
- Pricing moves are the most-copied and least-understood signal. Ask what it implies about
  their cost structure before assuming it implies anything about ours.

## Steps
1. Read `workspace/strategy/signals.md` and any new input. Gate: state the date range covered.
2. Split into moves and conditions. Gate: list each with actor and date.
3. Mark unsourced claims. Gate: list them explicitly, or state "all sourced".
4. Separate signal from noise, giving a reason for each dismissal. Gate: name what was dismissed and why.
5. Map each change to the theses it presses on. Gate: list thesis slugs; state "no bet touched" where true.
6. Log genuinely new signals with `strategy_log_signal`. Gate: confirm the cards were written.
7. State what would change our mind. Gate: run the checklist, report pass/fail.

## Output
A markdown read: `# Competitive read — <date range>`, then sections **What actually changed**,
**Moves** (actor, action, date, source), **Conditions**, **Noise (and why)**, **Bets under
pressure**, **What would change our mind**. Recommend "no response" where that is the honest call.

## Red Flags & Rationalizations
- "Competitors are moving upmarket" with no actor and no date — that is a mood, not a read.
- Every signal is treated as significant — nothing was dismissed, so nothing was analyzed.
- A press release is reported as a shipped capability.
- The read recommends a response to every move.

| Rationalization | Rebuttal |
| "They announced it, so it exists." | Announced and shipped are different columns. Say which one you have. |
| "It's directionally what they're doing." | Direction without an actor and a date cannot be checked later. |
| "We should respond to stay competitive." | Name the bet it threatens, or the honest answer is no response. |
| "Three sources said it, so it's confirmed." | Three outlets citing one press release is one source. |

## Validation
Pass when the checklist in `validation/quality-checklist.md` is all checked, every move has
actor + date + source, and at least one thing has been explicitly dismissed as noise with a
reason. Claims of "done" require the rendered read pasted, not described.

## Examples
- `examples/competitor-pricing-shift.md` — shows an announcement demoted to noise and a real condition surfaced.
