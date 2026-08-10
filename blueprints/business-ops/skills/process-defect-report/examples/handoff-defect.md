# Process defect — monthly-close / sales-to-finance handoff

## The pattern

Three slips on the same step, at threshold 3:

| Date | Commitment | Step |
|---|---|---|
| 2026-04-03 | `[[march-close]]` | sales-to-finance handoff |
| 2026-05-05 | `[[april-close]]` | sales-to-finance handoff |
| 2026-06-04 | `[[may-close]]` | sales-to-finance handoff |

Each slip was logged with a different reason: "waiting on signed order forms" (April),
"discount approvals not recorded" (May), "two deals booked in the wrong period" (June).
Three different reasons on the same step is itself the tell — the step is fragile, not the
month.

## Where it stalls

Not inside the close, and not inside sales. It stalls at the moment deal data changes hands
on the first business day of the month. Finance cannot begin until sales data is final, and
sales does not consider it final until finance asks. Each side is waiting for a signal the
other believes it already gave.

## The defect

**The monthly close has no defined completion point for its sales input.** The handoff is
triggered by finance asking rather than by a stated condition being met, so the input
arrives at a different moment and in a different state every month. Nothing marks "sales
data is closed" — so it is closed whenever someone happens to ask.

Note the absence of any name in the paragraph above. That is deliberate and it is the test.

## Process or capacity

**Process.** Tested the capacity hypothesis and rejected it: three different sales owners
were involved across the three months (Sam in April, Marcus in May, Sam again in June), and
finance had different staffing in June than in April. No common person, and no month where
the load was unusual. The step fails independent of who is standing on either side of it.

## The one change

**Move the cutoff decision earlier and make it explicit: sales data locks at 17:00 on the
last business day of the month, announced by the sales owner, whether or not finance has
asked.**

Just this. Not also a checklist, not also a new review meeting, not also a dashboard —
because next month the result has to be attributable to one change.

## What we should observe, and by when

By **2026-07-03** (July close, first business day):
- The close starts on day one without a request from finance for missing data.
- No slip is logged against the sales-to-finance handoff step.

If a slip is logged anyway, the cutoff was not the constraint and this report was wrong —
in which case the next candidate is the discount-approval recording step, which appeared in
one of the three reasons and has never been examined on its own.
