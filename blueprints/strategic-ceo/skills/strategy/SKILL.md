---
name: strategy
description: "Hold the long arc as an auditable record of bets and beliefs — append-only markdown cards under workspace/strategy/ for theses, assumptions, decisions, and signals, cross-linked by [[slug]] so a thesis resolves to the assumptions holding it up and the signals pressing on it. TRIGGER: a strategic bet stated or revised, an assumption named, a competitor or market signal noticed, a consequential decision made, or a request to review the strategy book. SKIP: operational commitments with an owner and a due date (that is ops) and day-to-day choices with no horizon."
version: 2.0.0
---

## Files

- `strategy/theses/<slug>.md` — one card per strategic bet.
- `strategy/assumptions/<slug>.md` — one card per assumption a thesis rests on.
- `strategy/decisions/<slug>.md` — one card per consequential decision.
- `strategy/signals/signals.md` — the single running signal log.

All paths are relative to the agent's own workspace, written with the ordinary
file tools (`read`, `write`, `ls`, `grep`). There is no strategy tool to call and
none is needed — this is the agent's own workspace.

## Contract

1. Every thesis, assumption, decision, and signal lands on its card at
   `strategy/<kind>/<slug>.md`.
2. Cards are **appended to, never rewritten**: read, add the entry at the end,
   write the whole file back. The superseded belief is the record's whole value.
3. A new card starts with exactly this header and nothing else:

   ```
   # <Title>

   - slug: <slug>
   - type: <kind>
   ```

4. Each entry is a UTC timestamp heading followed by `- key: value` lines,
   omitting every field with no value.
5. References between cards are written `[[slug]]`, never a bare name.
6. **Every assumption records what would falsify it.** An assumption with no
   `falsified_by` is an opinion, and is reported back as such.
7. A challenge appends to the assumption's own card with `status: challenged` —
   it never edits the original entry away.

## Knowledge

- **Thesis and assumption are separated for falsifiability.** A thesis is what is
  believed; an assumption is the specific claim that, if false, takes the thesis
  with it. Merged, nothing is ever checkable and a strategy dies of forgetting
  rather than of being wrong.
- **Strategy fails less from bad thinking than from forgetting** — the assumption
  nobody revisited, the competitor move noticed and dropped, the bet that quietly
  stopped being true. Appending is what makes revisiting possible.
- **A challenge is evidence, not a verdict.** Record it with its source and leave
  the verdict `under review` unless one was actually reached.
- **The slug is the identity.** Lowercase, non-alphanumeric runs → `-`, strip ends.
- **Get the timestamp from the clock**: run `date -u +'%Y-%m-%d %H:%M'` and use
  exactly what it returns. The prompt carries the date but not the time.
- Never invent a horizon, a confidence, or a source. An empty field is honest.

### Fields by kind

| Kind | Fields |
|---|---|
| `theses` | `name`, `claim`, `rationale`, `horizon`, `owner`, `status` (default `active`) |
| `assumptions` | `name`, `thesis` (link), `statement`, `falsified_by`, `confidence`, `status: holding` |
| `assumptions` (challenge) | `challenge`, `source`, `verdict` (default `under review`), `status: challenged` |
| `decisions` | `name`, `choice`, `tradeoff`, `reversible`, `thesis` (link), `revisit_on` |
| `signals` | `signal`, `actor`, `thesis` (link), `implication`, `source` |

Free-text context goes on a final `- note:` line.

## Steps

**To log a thesis, assumption, or decision:**

1. Run `date -u +'%Y-%m-%d %H:%M'`.
2. Derive the slug from the name.
3. `read` `strategy/<kind>/<slug>.md`; if absent, start from the header block.
4. Append a blank line, the `## <timestamp>` heading, and one `- key: value` line
   per non-empty field, then `- note:` if there is context.
5. `write` the whole file back.
6. Reply in one line. For an assumption with no `falsified_by`, add
   `⚠ nothing would falsify this — it is an opinion, not an assumption.`

**To challenge an assumption:**

1. Append a new entry to `strategy/assumptions/<assumption-slug>.md` carrying
   `challenge`, `source`, `verdict`, and `status: challenged`.
2. `grep` the theses for that assumption's slug and name every thesis resting on
   it in the reply — the point of the challenge is what it puts at risk.

**To log a signal:**

Append to the shared `strategy/signals/signals.md`, linking the thesis it bears on.

**To review the strategy book:**

1. `ls` `strategy/theses/`, `read` each, take the **last** value of each field.
2. For each thesis, `grep` the assumptions for its slug.
3. Report one block per thesis: its latest `status` and `claim`, then its
   assumptions, marking any whose latest `status` is `challenged`.
4. A thesis whose assumptions are all challenged is called out as a bet whose
   footing has gone.

## Red Flags & Rationalizations

| Thought | Reality |
|---|---|
| "The thesis and the assumption are the same claim." | Then nothing is falsifiable. Split them: the assumption is what can be checked. |
| "The old belief is wrong now, I'll replace it." | The superseded belief is the record's value. Append the new one. |
| "This assumption is obviously true." | Then `falsified_by` is easy to write. If it isn't, it's an opinion. |
| "The challenge settles it." | Evidence is not a verdict. Leave `under review` unless one was reached. |
| "I know roughly what time it is." | You know the date, not the time. Run `date -u` or the entry lands at 00:00. |
| "The review should show the first status I find." | Cards are append-only; the first is the oldest. Take the last. |

## Validation

A log is correct when:

- [ ] The card exists at `strategy/<kind>/<slug>.md` with the exact header block.
- [ ] Every prior entry is present, unmodified.
- [ ] The `## YYYY-MM-DD HH:MM` heading came from `date -u`, not `00:00`.
- [ ] Every assumption has a `falsified_by`, or was flagged as an opinion.
- [ ] A challenge appended to the assumption card with `status: challenged`.
- [ ] Every cross-reference is `[[slug]]`, not a bare name.

A review is correct when every thesis appears once with its latest status and
every challenged assumption beneath it is marked.

## Examples

**An assumption with a falsifier:**

```

## 2026-08-14 17:20
- name: Mid-market buys on compliance
- thesis: [[federal-first-wedge]]
- statement: Mid-market buyers will pay a premium for FedRAMP-grade controls
- falsified_by: Two consecutive quarters where compliance is absent from won-deal reasons
- confidence: medium
- status: holding
```

Reply: `Logged assumption 'Mid-market buys on compliance' under [[federal-first-wedge]]`

**An assumption with nothing that would falsify it:**

Reply: `Logged assumption 'AI adoption keeps growing' ⚠ nothing would falsify this — it is an opinion, not an assumption.`

**A review:**

```
federal-first-wedge — [active] Win federal first, then descend into mid-market
  - mid-market-buys-on-compliance: holding
  - procurement-cycles-shorten: ⚠ challenged (verdict: under review)
```
