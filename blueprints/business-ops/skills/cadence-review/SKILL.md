---
name: cadence-review
description: "Run the weekly operating review — what shipped, what slipped, what is blocked, and the decisions needed this week — ending in a short list of named asks rather than a status report. TRIGGER: the weekly review, a Monday planning session, 'where are we'. SKIP: a quick outstanding-items check (use owner-sweep) or diagnosing a recurring failure (use process-defect-report)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the review must clear before it is delivered.
- `examples/weekly-review.md` — a worked review ending in three decisions, not a status dump.

## Contract
Given the ops cards in `workspace/ops/`, produce a review such that:
1. It opens with **decisions needed this week** — the things that require a human to choose.
2. Closed loops are stated once and dropped; they do not accumulate week over week.
3. Slips are reported with their count to date, so a repeat is visible as a repeat.
4. Any step at or above the repeat threshold is escalated to `process-defect-report`, not
   re-chased as an individual miss.
5. Every open thread carries an owner and a date, or is flagged as lacking one.
6. It ends with at most **five** asks, each naming one person and one date.
Slips found during the review are written back with `ops_log_slip`.

## Knowledge
- A review exists to change a decision. If nothing would change as a result, the review is
  status theatre and should be shorter or skipped.
- Reporting activity is not reporting progress. "Worked on" is not a state; shipped,
  slipped, and blocked are.
- Closed items must actually leave the list. A list that only grows stops being read, and
  once it stops being read the review stops working.
- Five asks is a real limit. A review producing fifteen asks has produced none, because
  nobody will action fifteen.
- The cadence that survives is the short one that actually happens. Protect the format.

## Steps
1. Read status via `ops_status`. Gate: state counts for shipped, slipped, blocked, open.
2. Identify decisions that need a human this week. Gate: list them, or state "none".
3. List what closed since last review. Gate: confirm each is dropped from the running list.
4. Report slips with counts via `ops_slip_patterns`. Gate: state each slip's count to date.
5. Escalate any step at threshold to a defect report. Gate: name it, do not re-chase it.
6. Check every open thread for owner and date. Gate: flag any missing either.
7. Cut the asks to five, ordered by what unblocks the most. Gate: run the checklist, report pass/fail.

## Output
A markdown review: `# Operating review — week of <date>`, then sections **Decisions needed**,
**Closed this week**, **Slipped** (with count to date), **Blocked**, **Open**, **Asks**
(max five, each one person and one date). Decisions first, asks last, nothing in between
longer than a line per item.

## Red Flags & Rationalizations
- The review opens with a list of everything in flight — the decisions are buried.
- Closed items are still listed "for visibility" — the list is now permanent.
- A step slipping for the fourth time appears as a normal line item.
- There are eleven asks.
- An item reads "in progress" with no owner, no date, and no state.

| Rationalization | Rebuttal |
| "People want the full picture." | They want the decisions. The full picture is the cards. |
| "It closed but I'll keep it visible one more week." | That is how the list becomes unreadable. Drop it. |
| "All of these asks matter." | Then rank them and cut to five. Fifteen asks produce zero. |
| "It's still moving, just slowly." | Slow with a date is open. Slow with no date is slipped. |

## Validation
Pass when the checklist in `validation/quality-checklist.md` is all checked, the review opens
with decisions, and the ask list is five or fewer with a person and date on each. Claims of
"done" require the rendered review pasted, not described.

## Examples
- `examples/weekly-review.md` — shows a fourth slip escalated out of the review into a defect report.
