---
name: assumption-audit
description: "Sweep every active thesis for assumptions that have been contradicted, never tested, or quietly expired, and report which bets are now resting on nothing. TRIGGER: a strategy review, a quarterly planning session, or 'is what we believe still true'. SKIP: deciding a specific pending choice (use decision-brief) or logging what a competitor just did (use competitive-read)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the audit must clear before it is delivered.
- `examples/q3-audit.md` — a worked audit showing a contradicted assumption and its consequence.

## Contract
Given the strategy book in `workspace/strategy/`, produce an audit such that:
1. Every **active** thesis is listed with the assumptions holding it up.
2. Each assumption is classified: `holding`, `challenged`, `untested`, or `expired`.
3. An assumption with no falsification test is reported as `untested` — not as holding.
4. Every thesis resting on a challenged or untested assumption is named as **at risk**, with
   the specific assumption that puts it there.
5. Each at-risk thesis gets one concrete next action: retest, revise, or retire.
6. Nothing is invented — an assumption absent from the cards is absent from the audit.
This reads the strategy book and writes challenges; it does not retire theses on its own.

## Knowledge
- The dangerous assumption is not the one that was wrong. It is the one nobody checked,
  because it never generated an argument.
- An assumption with no falsification test cannot be audited. Treat the missing test as the
  finding, not as a detail.
- Assumptions expire. A belief formed against a market that has since moved is stale even
  if no single event contradicted it — check the date, not just the evidence.
- A thesis whose assumptions have all been challenged is not "under pressure". It is dead,
  and saying so plainly is the job.

## Steps
1. Read every thesis card and its status. Gate: list the active theses by slug.
2. For each, collect its linked assumptions. Gate: name any thesis with zero assumptions — that is a finding.
3. Classify each assumption. Gate: state the classification and the evidence or date behind it.
4. Record any newly contradicted assumption with `strategy_challenge`. Gate: confirm the card was written.
5. Map assumptions back to theses and mark the at-risk set. Gate: list at-risk theses with the causing assumption.
6. Give each at-risk thesis one action: retest, revise, retire. Gate: run the checklist, report pass/fail.

## Output
A markdown audit: `# Assumption audit — <date>`, then sections **At risk** (thesis, the
assumption that broke it, recommended action), **Untested** (assumptions with no
falsification test), **Holding**, and **Retired since last audit**. Lead with At risk.

## Red Flags & Rationalizations
- Every assumption comes back `holding` — the audit did not actually test anything.
- A thesis has no assumptions listed and this is not reported as a finding.
- The audit hedges an obviously dead thesis as "worth monitoring".
- Dates are missing, so "expired" could not have been assessed.

| Rationalization | Rebuttal |
| "No new evidence, so it still holds." | No evidence is not confirming evidence. That is `untested`. |
| "The thesis is directionally right." | Directionally right is how a dead bet survives a review. Name the specific assumption. |
| "We already know this one is shaky." | Then it is `challenged` in the record, not folklore in someone's head. |
| "Retiring it wastes the work we did." | Sunk cost. The work is spent either way. |

## Validation
Pass when the checklist in `validation/quality-checklist.md` is all checked, every active
thesis appears exactly once, and each at-risk finding names the specific assumption and
evidence. Claims of "done" require the rendered audit pasted, not described.

## Examples
- `examples/q3-audit.md` — shows an untested assumption promoted to the lead finding.
