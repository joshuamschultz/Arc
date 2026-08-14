---
name: ops
description: "Track who owes what by when, and notice when the same process step keeps slipping. Writes append-only markdown cards under workspace/ops/ — commitments, processes, blockers, slips — cross-linked by [[slug]] so a blocker resolves to the commitment it holds up and a slip resolves to the step that produced it. TRIGGER: any promise with an owner and a date, anything stuck waiting on a decision, a due date that moved, or a request for what is open/overdue/repeatedly slipping. SKIP: one-off notes with no owner and no date — those are memory, not commitments."
version: 2.0.0
---

## Files

- `ops/commitments/<slug>.md` — one card per commitment.
- `ops/processes/<slug>.md` — one card per recurring process.
- `ops/blockers/<slug>.md` — one card per blocker.
- `ops/slips/slips.md` — the single running slip log.

All paths are relative to the agent's own workspace, written with the ordinary
file tools (`read`, `write`, `ls`, `grep`). There is no ops tool to call and none
is needed — this is the agent's own workspace.

## Contract

1. Every commitment, process, blocker, and slip lands on its card at
   `ops/<kind>/<slug>.md`.
2. Cards are **appended to, never rewritten**: read, add the entry at the end,
   write the whole file back.
3. A new card starts with exactly this header and nothing else:

   ```
   # <Title>

   - slug: <slug>
   - type: <kind>
   ```

4. Each entry is a UTC timestamp heading followed by `- key: value` lines,
   omitting every field with no value.
5. References to another card are written `[[slug]]`, never a bare name.
6. **Blocked and late are recorded as different states.** A commitment past due
   is `status: open` and overdue; one waiting on someone else is a blocker card
   with a named `decider`.
7. A slip appends to BOTH the commitment's card and `ops/slips/slips.md`.

## Knowledge

- **Blocked and late need opposite responses.** Late needs a push to the owner;
  blocked needs a decision from somebody else. Collapsing them into "not done" is
  how a blocker sits for three weeks while someone politely re-pings the person
  who was never the obstacle.
- **A slip is data, not an apology.** Logging it against the *process step* is
  what turns three slips into a visible defect instead of three bad weeks.
- **The slug is the identity.** Lowercase, non-alphanumeric runs → `-`, strip
  ends. The same commitment must always produce the same slug, or its history
  forks and neither half is true.
- **Get the timestamp from the clock**: run `date -u +'%Y-%m-%d %H:%M'` and use
  exactly what it returns. The prompt carries the date but not the time, so a
  remembered timestamp lands at `00:00`.
- A commitment with no `due` cannot be overdue and cannot be chased. Record it,
  and say that the date is missing.
- Never invent an owner or a date. An empty field is honest.

### Fields by kind

| Kind | Fields |
|---|---|
| `commitments` | `commitment`, `owner`, `due`, `process` (link), `status` (default `open`) |
| `processes` | `name`, `owner`, `cadence`, `steps`, `handoffs` |
| `blockers` | `blocker`, `blocked_on`, `decider`, `since` (default today), `commitment` (link), `status: blocked` |
| `slips` | `slipped`, `process` (link), `step`, `reason`, `new_due`, `status: slipped` |

Free-text context goes on a final `- note:` line.

## Steps

**To log a commitment, process, or blocker:**

1. Run `date -u +'%Y-%m-%d %H:%M'`.
2. Derive the slug from the name.
3. `read` `ops/<kind>/<slug>.md`; if absent, start from the header block.
4. Append a blank line, the `## <timestamp>` heading, and one `- key: value` line
   per non-empty field, then `- note:` if there is context.
5. `write` the whole file back.
6. Reply in one line naming what was logged and its owner/due.

**To log a slip:**

1. Append the slip fields to `ops/commitments/<commitment-slug>.md`.
2. Append the same fields to `ops/slips/slips.md`.
3. Reply naming the new due date and the process step that produced the slip.

**To report status (open commitments):**

1. `ls` `ops/commitments/`, `read` each card, take the **last** value of each field.
2. Drop anything whose latest `status` is `done`.
3. Sort overdue first (by `due`, earliest first), then dated, then undated.
4. Group by `owner`, and mark any commitment that has a blocker card referencing
   it with `⛔ blocked on <decider>`.

**To find slip patterns:**

1. `read` `ops/slips/slips.md`.
2. Count entries per `process` + `step` pair.
3. Report every pair at or above the threshold (default 3), most frequent first,
   as a defect in that step — not as bad luck.

## Red Flags & Rationalizations

| Thought | Reality |
|---|---|
| "It's late, I'll just mark it not done." | Late and blocked need opposite actions. Log a blocker with its decider. |
| "I'll rewrite the card so it reads cleanly." | That deletes the history that makes slips visible. Append. |
| "The slip is obvious, no need to log it." | Three unlogged slips look like bad luck; three logged ones name a broken step. |
| "No due date was given, I'll estimate one." | An invented date becomes someone's commitment. Leave it empty and say so. |
| "I know roughly what time it is." | You know the date, not the time. Run `date -u` or the entry lands at 00:00. |
| "Status should show the first value I find." | Cards are append-only; the first is the oldest. Take the last. |

## Validation

A log is correct when:

- [ ] The card exists at `ops/<kind>/<slug>.md` with the exact header block.
- [ ] Every prior entry is present, unmodified.
- [ ] The `## YYYY-MM-DD HH:MM` heading came from `date -u`, not `00:00`.
- [ ] Blocked items are blocker cards with a named `decider`, not overdue commitments.
- [ ] A slip appears in BOTH the commitment card and `ops/slips/slips.md`.

A status report is correct when every open commitment appears exactly once, with
its latest values, overdue first, and blockers called out.

## Examples

**A commitment:**

> "Dana owes the vendor security review by the 20th, part of the onboarding process."

Appends to `ops/commitments/vendor-security-review.md`:

```

## 2026-08-14 17:20
- commitment: Vendor security review
- owner: Dana
- due: 2026-08-20
- process: [[vendor-onboarding]]
- status: open
```

Reply: `Logged commitment 'Vendor security review' — Dana, due 2026-08-20`

**A blocker (not an overdue commitment):**

Reply: `Logged blocker 'Vendor security review' — blocked on legal sign-off, decider: Priya (since 2026-08-14)`

**A slip pattern:**

```
Repeated slips (threshold 3):
- vendor-onboarding / security review: 4 slips — this step is a defect, not bad luck
```
