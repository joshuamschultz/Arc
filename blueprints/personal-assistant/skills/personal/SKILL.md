---
name: personal
description: "Hold one person's people, promises, and preferences as append-only markdown cards under workspace/personal/ — cross-linked by [[slug]] so 'what's going on with the house' resolves to the contractor, the quote, and the call that was promised. TRIGGER: any mention of a person in their life, any promise made in either direction, any standing rule about how they want things done, and any request for open loops. SKIP: work commitments with a business owner and process (that is ops), and passing facts with no person, promise, or rule attached."
version: 2.0.0
---

## Files

- `personal/people/<slug>.md` — one card per person.
- `personal/commitments/<slug>.md` — one card per promise, either direction.
- `personal/preferences/preferences.md` — the single running preferences card.

All paths are relative to the agent's own workspace, written with the ordinary
file tools (`read`, `write`, `ls`, `grep`). There is no personal tool to call and
none is needed — this is the agent's own workspace.

## Contract

1. Every person, promise, and preference lands on its card at
   `personal/<kind>/<slug>.md`.
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
5. A person referenced from a commitment is written `[[slug]]`, never a bare name.
6. **Every commitment carries a direction**: `i-owe` or `owed-to-me`. It is never
   omitted; unset defaults to `i-owe`.
7. Preferences all append to the one `preferences.md` card.

## Knowledge

- **Direction is the whole point of a personal commitment.** "Not done" collapses
  *I owe my sister a call* and *the contractor owes me a quote* into one
  undifferentiated pile, and the two need opposite actions: one is a task, the
  other is a chase.
- **A preference is durable and usually unprompted.** How they like to be spoken
  to, never scheduling before 10am — it is worth its own card precisely because
  nobody will repeat it, and forgetting it reads as not listening.
- **The slug is the identity.** Lowercase, non-alphanumeric runs → `-`, strip
  ends. `Aunt Ruth` → `aunt-ruth`, every time.
- **Get the timestamp from the clock**: run `date -u +'%Y-%m-%d %H:%M'` and use
  exactly what it returns. The prompt carries the date but not the time.
- Record `last_contact` when a conversation actually happened; it is what makes
  "who have I gone quiet on" answerable later.
- Never invent a date, a relationship, or a promise that was not made.

### Fields by kind

| Kind | Fields |
|---|---|
| `people` | `name`, `relationship`, `context`, `last_contact` |
| `commitments` | `commitment`, `direction` (`i-owe` \| `owed-to-me`, default `i-owe`), `person` (link), `due`, `status` (default `open`) |
| `preferences` | `preference`, `area`, `strength` |

Free-text context goes on a final `- note:` line.

## Steps

**To log a person or a commitment:**

1. Run `date -u +'%Y-%m-%d %H:%M'`.
2. Derive the slug from the name (a commitment slugs from its text).
3. `read` `personal/<kind>/<slug>.md`; if absent, start from the header block.
4. Append a blank line, the `## <timestamp>` heading, and one `- key: value` line
   per non-empty field, then `- note:` if there is context.
5. `write` the whole file back.
6. Reply in one line naming what was logged, including the direction for a
   commitment.

**To log a preference:**

Append the same way to `personal/preferences/preferences.md` — one shared card,
never one per preference.

**To list open loops:**

1. `ls` `personal/commitments/`, `read` each, take the **last** value of each field.
2. Drop anything whose latest `status` is `done`.
3. Split into two groups: **I owe** and **owed to me**.
4. Within each, overdue first (earliest `due`), then dated, then undated.
5. Report one line per commitment with the person and the due date, marking
   undated ones plainly — they cannot be chased until they have a date.

## Red Flags & Rationalizations

| Thought | Reality |
|---|---|
| "It's a promise either way, direction hardly matters." | It is the difference between a task and a chase. Always record it. |
| "That preference is obvious, no need to write it." | Nobody repeats a preference. Unwritten, it reads as not listening. |
| "I'll tidy the card while I'm here." | Appending is what keeps the thread; rewriting deletes it. |
| "No due date given, I'll pick something reasonable." | An invented date becomes a broken promise. Leave it empty and say so. |
| "I know roughly what time it is." | You know the date, not the time. Run `date -u` or the entry lands at 00:00. |
| "Open loops should show the first status I find." | Cards are append-only; the first is the oldest. Take the last. |

## Validation

A log is correct when:

- [ ] The card exists at `personal/<kind>/<slug>.md` with the exact header block.
- [ ] Every prior entry is present, unmodified.
- [ ] The `## YYYY-MM-DD HH:MM` heading came from `date -u`, not `00:00`.
- [ ] Every commitment has a `direction`.
- [ ] Every person reference is `[[slug]]`, not a bare name.
- [ ] Preferences went to the shared `preferences.md`, not a new card.

An open-loops report is correct when both directions are shown separately and
every open commitment appears exactly once with its latest values.

## Examples

**A promise owed to them:**

> "The contractor said he'd send the quote by Friday."

Appends to `personal/commitments/contractor-quote.md`:

```

## 2026-08-14 17:20
- commitment: Send the kitchen quote
- direction: owed-to-me
- person: [[contractor-mike]]
- due: 2026-08-16
- status: open
```

Reply: `Logged commitment 'Send the kitchen quote' — owed to you by Mike, due 2026-08-16`

**A preference:**

Reply: `Noted preference: no meetings before 10am (area: schedule, strength: firm)`

**Open loops:**

```
I owe:
- Call Aunt Ruth — [[aunt-ruth]], due 2026-08-12 (overdue)
Owed to me:
- Send the kitchen quote — [[contractor-mike]], due 2026-08-16
```
