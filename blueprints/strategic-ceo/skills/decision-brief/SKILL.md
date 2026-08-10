---
name: decision-brief
description: "Turn a pending choice into a decision brief — what would have to be true, the strongest case against, reversibility, and a named recommendation with its cost. TRIGGER: a decision is on the table ('should we move upmarket', 'do we take the raise', 'pick between A and B'). SKIP: auditing whether existing beliefs still hold (use assumption-audit) or reading what competitors did (use competitive-read)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the brief must clear before it is delivered.
- `examples/upmarket-move.md` — a worked brief for an irreversible go-upmarket choice.

## Contract
Given a pending decision, produce a brief such that:
1. The decision is stated as a choice between named, concrete options — never "should we consider X".
2. For each option, **what would have to be true** is listed as testable conditions.
3. The strongest case AGAINST the recommendation is stated before the recommendation.
4. Reversibility is classified: reversible (decide fast, learn) or irreversible (slow down).
5. Exactly ONE recommendation is named, with the tradeoff it accepts stated explicitly.
6. Every factual claim is grounded in a strategy card, memory, or a named source; gaps read `unknown`.
Nothing is decided on the operator's behalf — the brief makes the tradeoff visible so the
decision stays theirs.

## Knowledge
- Reversible and irreversible decisions deserve opposite treatment. Misclassifying a
  reversible choice as irreversible is the most common way good teams get slow.
- "What would have to be true" converts an argument into a set of checks. It is the
  difference between a debate and a decision.
- Sunk cost is never an argument. What was already spent says nothing about what to do next.
- A recommendation that claims no cost has not been thought through.

## Steps
1. State the decision as competing options. Gate: list the options; refuse a one-option "decision".
2. Read the relevant theses, assumptions, and signals from `workspace/strategy/`. Gate: list the card slugs used.
3. For each option, write what would have to be true. Gate: state the conditions as checkable claims.
4. Steelman the case against your intended recommendation. Gate: write it out in full first.
5. Classify reversibility and say what that implies for pace. Gate: state reversible or irreversible.
6. Name one recommendation and the tradeoff it accepts. Gate: run the quality checklist, report pass/fail.
7. Log the decision with `strategy_log_decision` once the operator commits. Gate: confirm the card was written.

## Output
A markdown brief: `# Decision — <name>`, then sections **The choice**, **What would have to
be true**, **The case against**, **Reversibility**, **Recommendation**, **What we accept**,
**Revisit when**. Unknowns are shown as `unknown`, never guessed.

## Red Flags & Rationalizations
- The brief presents three options and recommends none — that is a survey, not a brief.
- The case against is one weak sentence — the steelman was skipped.
- No tradeoff is named — the recommendation is being sold, not argued.
- A number or competitor claim appears that is in no card and has no source — it was invented.

| Rationalization | Rebuttal |
| "Both options are defensible, so I'll present both." | Present both, then recommend one. Judgment is the deliverable. |
| "The downside is obvious, no need to write it." | If it is obvious, writing it costs one line. Write it. |
| "This is too big to call reversible or not." | Size is not the axis. Ask only: can we undo it, and at what cost? |
| "Let's revisit once we have more data." | Name the specific data and the date, or that is a decision to drift. |

## Validation
Pass when the checklist in `validation/quality-checklist.md` is all checked and a reader who
disagrees with the recommendation can still see exactly why it was made. Claims of "done"
require the rendered brief pasted, not described.

## Examples
- `examples/upmarket-move.md` — an irreversible choice showing the steelman and the named tradeoff.
