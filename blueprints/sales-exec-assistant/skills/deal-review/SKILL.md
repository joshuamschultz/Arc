---
name: deal-review
description: "Assess one deal's health and produce a candid verdict — is it real, where is the risk, and what is the single next action to de-risk it. Scores momentum, multi-threading, and stage fit against the account history in memory. TRIGGER: 'is the Northwind deal real?', 'review my pipeline', forecast prep, or a deal that has gone quiet. SKIP: preparing for a specific upcoming call (use pre-call-brief) or listing overdue follow-ups across every account (use follow-up-sweep)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar a review must clear.
- `examples/northwind-review.md` — a worked review of a stalled deal.

## Contract
Given a named deal, produce a health verdict such that:
1. A one-word verdict is given: **healthy / at-risk / slipping / stalled**.
2. Momentum is assessed from the record: is there a scheduled next step, and how long in-stage.
3. Threading is assessed: how many contacts, and whether the economic buyer is engaged.
4. The top risk is named in one sentence, grounded in a card or an insight-memory pattern.
5. Exactly ONE next action to de-risk the deal is prescribed.
Nothing else is modified (reviewing does not change the deal card).

## Knowledge
- No scheduled next step is the single strongest slip predictor — weight it heaviest.
- A single-threaded deal (champion only, no economic buyer) is fragile regardless of warmth.
- Time-in-stage beyond the account's norm is a slipping signal; check insight memory for the pattern.
- A "verbal yes" with no procurement motion is not procurement.

## Steps
1. Read the deal card and its linked company + contact cards. Gate: list the slugs read.
2. Check for a scheduled next step and time-in-stage. Gate: state both plainly.
3. Count threads; confirm whether the economic buyer is engaged. Gate: name the contacts + roles.
4. Query insight memory for a matching risk pattern. Gate: cite the insight or state "none matched".
5. Render the verdict + single next action per `## Output`. Gate: run the quality checklist.

## Output
Markdown: `# Deal review — <deal>` then **Verdict** (one word + one line), **Momentum**,
**Threading**, **Top risk** (one sentence), **Do this next** (one action). No hedging list.

## Red Flags & Rationalizations
- The review ends with five "next steps" — that is avoidance, not a decision.
- Verdict is "healthy" while there is no scheduled next step — contradiction.
- The top risk is generic ("needs follow-up") rather than specific to this deal.

| Rationalization | Rebuttal |
| "The champion loves us, so it's healthy." | Warmth without a buyer and a next step is at-risk. |
| "It's been in procurement a while, that's normal." | Check the account's stage norm before excusing it. |

## Validation
Pass when the checklist is fully checked and the verdict is defensible from the cited cards.
A "done" claim requires the rendered review pasted.

## Examples
- `examples/northwind-review.md` — a stalled deal scored at-risk with one corrective action.
