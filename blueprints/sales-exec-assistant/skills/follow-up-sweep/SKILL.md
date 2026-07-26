---
name: follow-up-sweep
description: "Sweep the whole book for dropped balls — commitments due or overdue, deals with no scheduled next step, and champions gone quiet — and return a ranked action list. Powers the morning briefing. TRIGGER: 'what am I forgetting?', 'what's overdue?', the daily briefing, or a weekly pipeline hygiene pass. SKIP: prepping one specific call (use pre-call-brief) or judging a single deal's health in depth (use deal-review)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the sweep must clear.
- `examples/monday-sweep.md` — a worked morning sweep.

## Contract
Given the current book of business, produce a ranked action list such that:
1. Every commitment that is due or overdue is listed, most-overdue first, with who/what/when.
2. Every active deal with no scheduled next step is flagged.
3. Every deal whose champion has gone quiet beyond the norm is flagged.
4. Items are ranked by the identity's priority order (customer commitments → deals at risk → warm touches).
5. Each item carries a single concrete action; the list is capped at the top ~7 so it is actionable.
Nothing else is modified (the sweep reads; it does not close or reschedule items itself).

## Knowledge
- Rank customer-facing commitments above internal ones; overdue above upcoming.
- "No next step" on an active deal is itself an overdue item — surface it even if nothing is explicitly late.
- A long list is ignored; cap it and say how many were dropped below the cut.
- Silence from a previously-active champion is a signal, not neutral (insight memory).

## Steps
1. Read all commitments and deal cards from `workspace/crm/`. Gate: state counts scanned.
2. Compute due/overdue commitments and no-next-step deals. Gate: list raw findings with dates.
3. Detect quiet champions (last touch beyond norm). Gate: name them or state "none".
4. Rank by priority order and cap at ~7. Gate: state how many were dropped below the cut.
5. Render the ranked action list per `## Output`. Gate: run the quality checklist.

## Output
Markdown: `# Follow-up sweep — <date>` then a numbered list, each item
`<what> — <who/when> → <one action>`, ranked. A final line: `(N more below the cut)`.

## Red Flags & Rationalizations
- The list is 25 items long — nobody acts on it; it was not ranked or capped.
- A deal with no next step is missing because "nothing was technically overdue".
- Internal housekeeping outranks an overdue customer commitment.

| Rationalization | Rebuttal |
| "I'll list everything to be safe." | An unranked dump is ignored; rank and cap it. |
| "No date attached, so it's not overdue." | A commitment with no date is still owed — surface it to get a date. |

## Validation
Pass when the checklist is fully checked, the list is ranked and capped, and every item has
one concrete action. A "done" claim requires the rendered list pasted.

## Examples
- `examples/monday-sweep.md` — a Monday sweep showing ranking and the below-the-cut line.
