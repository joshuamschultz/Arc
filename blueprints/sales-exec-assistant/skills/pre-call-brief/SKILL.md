---
name: pre-call-brief
description: "Assemble a one-page pre-call brief for a sales conversation from CRM memory — who is on the call, the deal's state, history, open commitments, and the one outcome to drive. TRIGGER: the executive has a call/meeting coming up ('prep me for the Acme call', 'what do I need before the Northwind QBR', 'brief me on Jane'). SKIP: reviewing a deal's health for forecasting (use deal-review) or sweeping overdue follow-ups across the book (use follow-up-sweep)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the brief must clear before it is delivered.
- `examples/acme-qbr.md` — a worked brief for a renewal QBR.

## Contract
Given a named upcoming call (a contact and/or company), produce a one-page brief such that:
1. Every person expected on the call is named with role and deal-role (champion / buyer / blocker).
2. The deal state is stated: stage, value, next step + owner, and the last meeting's outcome.
3. Every open commitment in either direction is listed with who owes what by when.
4. Exactly ONE primary outcome to drive on the call is named.
5. Every fact is grounded in a CRM card or memory — nothing invented; gaps are marked "unknown".
Nothing else is modified (this reads memory; it does not change deals).

## Knowledge
- Pull from the CRM cards (`workspace/crm/`) and memory, not from assumptions.
- A call with no single named outcome drifts — force the one thing.
- Single-threaded deals (one contact) are a risk; note it when only one name exists.

## Steps
1. Resolve the company + contacts from memory; read their cards. Gate: list the card slugs used.
2. Read the deal card: stage, value, next step, champion, risks. Gate: paste the deal's current line.
3. Collect open commitments touching this account. Gate: list them or state "none open".
4. Name the single primary outcome and up to three talking points. Gate: state the outcome.
5. Render the brief per `## Output`. Gate: run the quality checklist and report pass/fail.

## Output
A one-page markdown brief: `# Brief — <company> call <date>`, then sections **Who's on the
call**, **Deal state**, **Open commitments**, **History (last touch)**, **Drive this**,
**Talking points**. Unknowns are shown as `unknown`, never guessed.

## Red Flags & Rationalizations
- The brief names no single outcome — it is a data dump, not a brief.
- A person, amount, or date appears that is in no card — it was invented.
- The brief is longer than one page — signal is buried.

| Rationalization | Rebuttal |
| "I'll infer the stage from context." | Stages drive forecasts; read the card or mark unknown. |
| "Three outcomes are better than one." | One call, one primary outcome; the rest are talking points. |

## Validation
Pass when the checklist in `validation/quality-checklist.md` is all checked and a fresh
reader could walk into the call from the brief alone. Claims of "done" require the rendered
brief pasted, not described.

## Examples
- `examples/acme-qbr.md` — a renewal QBR brief showing the unknown-marking and single-outcome rules.
