---
name: open-loop-sweep
description: "Sweep every open promise in both directions — what the operator owes and what is owed to them — closing what is settled and surfacing what has quietly gone stale. TRIGGER: a weekly sweep, 'what am I forgetting', or a nagging sense that something has been dropped. SKIP: the short morning brief (use daily-brief) or prepping for one person (use person-brief)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the sweep must clear before it is delivered.
- `examples/weekly-sweep.md` — a worked sweep that closes three loops and reopens one.

## Contract
Given the commitments in `workspace/personal/`, produce a sweep such that:
1. Promises are split by direction: **you owe** and **owed to you**. They need different actions.
2. Anything already settled is closed with `pa_log_commitment` and stated once, then dropped.
3. Commitments with no date are surfaced separately — they can never become late, so they
   never surface on their own.
4. Stale items (open well past their date, with no movement) are named as stale, with the age.
5. Each open item gets one next action, or an explicit "waiting on <person>".
6. Nothing is invented, and nothing is closed without confirmation from the operator.
Settled items are written back; nothing else is modified.

## Knowledge
- The two directions need opposite handling. What you owe needs doing; what is owed to you
  needs a nudge to someone else, and forgetting that is how people quietly absorb other
  people's dropped promises.
- An undated commitment is invisible to every other surface. The sweep is the only place it
  gets caught, which makes surfacing it the sweep's most valuable job.
- Closing loops out loud matters as much as tracking them. A list that only grows stops
  being read, and then the tracking was pointless.
- Do not close something on the operator's behalf because it looks done. Ask once, then close.

## Steps
1. Read all open loops via `pa_open_loops`. Gate: state counts per direction.
2. Identify anything that appears settled. Gate: list them and ask for confirmation before closing.
3. Close confirmed items with `pa_log_commitment` status=done. Gate: confirm the cards were written.
4. Split remaining items by direction. Gate: confirm no item appears in both.
5. Surface undated commitments. Gate: list them, or state "all dated".
6. Flag stale items with their age. Gate: state the staleness bar used.
7. Give each open item one next action or a named "waiting on". Gate: run the checklist, report pass/fail.

## Output
A markdown sweep: `# Open loops — <date>`, then **Closed this sweep** (one line each, then
gone), **You owe**, **Owed to you**, **No date set**, **Stale**. Each open item gets one
next action. Keep the whole thing scannable.

## Red Flags & Rationalizations
- Both directions are merged into one list — the reader cannot tell what needs doing versus nudging.
- An item was closed because it looked done, without asking.
- Closed items are retained "for the record" — the list now only grows.
- Undated items are listed among dated ones, where they disappear.

| Rationalization | Rebuttal |
| "It's obviously finished." | Obvious is not confirmed. Ask once, then close it. |
| "I'll keep closed ones visible this week." | That is how the list becomes unreadable. State once, drop. |
| "No date means it's not urgent." | It means it can never become late. That is worse, not better. |
| "They're waiting on someone, nothing to do." | Then the action is a nudge with a name and a date. |

## Validation
Pass when the checklist in `validation/quality-checklist.md` is all checked, every open item
has one next action, and closed items appear once and are dropped. Claims of "done" require
the rendered sweep pasted, not described.

## Examples
- `examples/weekly-sweep.md` — shows an undated loop surfaced and a "looks done" item confirmed first.
