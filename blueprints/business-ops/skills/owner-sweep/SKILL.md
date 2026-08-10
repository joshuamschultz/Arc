---
name: owner-sweep
description: "Sweep every open commitment for the three states that stall work — no owner, no date, or blocked on someone who has not been asked — and produce a specific ask for each. TRIGGER: 'what's outstanding', a status check, or the start of the operating week. SKIP: running the full weekly review with decisions (use cadence-review) or turning repeat misses into a process fix (use process-defect-report)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the sweep must clear before it is delivered.
- `examples/monday-sweep.md` — a worked sweep separating blocked from late.

## Contract
Given the commitments in `workspace/ops/`, produce a sweep such that:
1. **Blocked** and **late** are reported separately and never merged into "not done".
2. Every blocked item names the person who must decide, and how long it has been blocked.
3. Every commitment with no owner or no date is surfaced as a defect, not quietly listed.
4. Each item carries one specific ask, addressed to one named person, with a date.
5. Items are ordered by what stalls other work first, not by age or alphabet.
6. Nothing is invented — an owner absent from the cards is reported `UNOWNED`, never guessed.
Findings are written back with `ops_log_blocker` / `ops_log_commitment` as gaps are closed.

## Knowledge
- Blocked needs an escalation to a decider; late needs a push to the owner. Treating them
  the same is how a blocker sits a week while the wrong person is politely re-asked.
- "I thought you had it" is the single most common root cause. An unowned commitment is not
  a tracking gap, it is the finding.
- A general reminder produces nothing. A specific ask with a name and a date produces a reply.
- Age is not priority. The two-day blocker holding up four people outranks the three-week
  item nobody is waiting on.

## Steps
1. Read open commitments via `ops_status`. Gate: state the counts per bucket.
2. Split blocked from late. Gate: confirm no item appears in both.
3. Surface unowned and undated commitments. Gate: list them, or state "all owned and dated".
4. For each blocked item, name the decider and days blocked. Gate: name a decider or flag its absence.
5. Order by what unblocks the most downstream work. Gate: state the ordering rationale in one line.
6. Write one specific ask per item. Gate: every ask names a person and a date.
7. Run the quality checklist. Gate: report pass/fail.

## Output
A markdown sweep: `# Owner sweep — <date>`, then sections **Blocked — needs a decision**
(item, decider, days blocked, the ask), **Overdue** (item, owner, days late, the ask),
**Missing owner or date**, **On track**. Lead with Blocked. Keep On track to one line each.

## Red Flags & Rationalizations
- Blocked and overdue appear in one merged list — the distinction that drives the action was lost.
- An ask reads "follow up on this" — no name, no date, no chance.
- An owner is inferred from who talked about it last.
- The sweep is a complete inventory with no ordering — a list, not a sweep.

| Rationalization | Rebuttal |
| "It's obvious who owns this." | Then it costs nothing to name them. Unnamed is unowned. |
| "I'll chase the owner on the blocked one." | The owner is not the constraint. Escalate to the decider. |
| "No date yet, it's still being scoped." | "Scoped by <date>" is itself a commitment. Get that date. |
| "Everything is listed, so the sweep is done." | An unordered list moves nothing. Lead with what stalls others. |

## Validation
Pass when the checklist in `validation/quality-checklist.md` is all checked and every item
carries an ask naming one person and one date. Claims of "done" require the rendered sweep
pasted, not described.

## Examples
- `examples/monday-sweep.md` — shows a blocker escalated past its nominal owner to the decider.
