---
name: crm
description: "Maintain the revenue book of business as interlinked markdown cards under workspace/crm/ — contacts, companies, deals, meetings, commitments — and read the pipeline back. Cards are append-only and cross-linked by [[slug]] so a deal resolves to its company, champion, and meetings. TRIGGER: any mention of a person, account, deal, meeting outcome, or promise made either way ('log this', 'we agreed to', 'they moved to procurement'), and any request for the open pipeline. SKIP: judging whether a deal is healthy (use deal-review), preparing for a specific call (use pre-call-brief), or listing what is overdue across accounts (use follow-up-sweep) — those READ these cards, they do not write them."
version: 2.0.0
---

## Files

- `crm/contacts/<slug>.md` — one card per person.
- `crm/companies/<slug>.md` — one card per account.
- `crm/deals/<slug>.md` — one card per opportunity.
- `crm/meetings/<title-slug>-YYYYMMDD.md` — one card per meeting.
- `crm/commitments/commitments.md` — the single running commitments log.

All paths are relative to the agent's own workspace, written with the ordinary
file tools (`read`, `write`, `ls`, `grep`). There is no CRM tool to call and none
is needed — this is the agent's own workspace.

## Contract

Log to the CRM such that:

1. Every entity named lands on its card at `crm/<kind>/<slug>.md`.
2. The card is **appended to, never rewritten**: read it, add the new entry at
   the end, write the whole file back. Prior entries are the account's history;
   losing one loses the story.
3. A new card starts with exactly this header and nothing else:

   ```
   # <Title>

   - slug: <slug>
   - type: <kind>
   ```

4. Each entry is a UTC timestamp heading followed by `- key: value` lines,
   omitting every field with no value.
5. A reference to another entity is written `[[slug]]`, never a bare name, so the
   cards stay a graph.
6. A deal logged with no `next_step` is reported back with a warning.
7. The reply names what was logged in one line. Nothing else in the workspace is
   modified.

## Knowledge

- **The slug is the identity.** Lowercase the name, replace every run of
  non-alphanumeric characters with `-`, strip leading/trailing `-`. `3G Lighting`
  → `3g-lighting`; `Helen Li` → `helen-li`. A second spelling silently forks one
  account into two, and neither half then tells the truth.
- **Append-only is why the pipeline can be read at all.** The current stage is
  the *last* `- stage:` in the file; every earlier one is history worth keeping.
- **No scheduled next step is the strongest slip predictor there is** — that is
  why a deal without one is flagged out loud rather than logged quietly.
- Commitments are the one exception to one-card-per-entity: they all append to
  `crm/commitments/commitments.md`. When a commitment names a deal, also append
  `- commitment:` and `- due:` to that deal's card, so the promise is visible to
  anyone reading the deal.
- **Get the timestamp from the clock, not from memory**: run
  `date -u +'%Y-%m-%d %H:%M'` and use exactly what it returns. The prompt
  carries the date but not the time, so a timestamp written from memory lands
  at `00:00` and every card logged that day sorts as if it happened at midnight.
- Never invent a stage, value, or date that was not said. An empty field is
  honest; a guessed one becomes someone's forecast.

### Fields by kind

| Kind | Fields |
|---|---|
| `contacts` | `name`, `role`, `employer` (link), `deal_role`, `email` |
| `companies` | `name`, `industry` |
| `deals` | `name`, `company` (link), `stage`, `value`, `next_step`, `next_step_owner`, `champion` (link) |
| `meetings` | `title`, `attendees`, `deal` (link), `follow_ups` |
| `commitments` | `commitment`, `owner`, `due`, `deal` (link) |

Free-text context goes on a final `- note:` line.

## Steps

**To log an entity:**

1. Run `date -u +'%Y-%m-%d %H:%M'` to get the real timestamp.
2. Derive the slug from the name (see Knowledge).
3. `read` `crm/<kind>/<slug>.md`. If it does not exist, start from the header
   block in the Contract.
4. Append a blank line, then `## <UTC timestamp>`, then one `- key: value` line
   per non-empty field for that kind, then `- note: <context>` if there is any.
5. `write` the whole file back — header, every prior entry, and the new one.
6. Reply in one line naming what was logged. For a deal with no `next_step`, add
   `⚠ no next step set — schedule one.`

**To summarize the pipeline:**

1. `ls` `crm/deals/` to list the deal cards.
2. `read` each one.
3. For each, take the **last** `- stage:` and the **last** `- next_step:`.
4. Report one line per deal:
   `- <slug>: [<stage>] next: <next_step>`, using `no next step ⚠` when absent,
   under a `Pipeline:` heading.
5. With no deal cards at all, reply `No deals logged yet.`

## Red Flags & Rationalizations

| Thought | Reality |
|---|---|
| "I'll just write the card fresh, it's cleaner." | That deletes the account's history. Read first, append, write the whole file back. |
| "The name is close enough to the existing card." | Close enough forks the account. Derive the slug by the rule and match it exactly. |
| "They didn't say a stage, I'll infer one." | An invented stage becomes someone's forecast. Leave it out. |
| "I'll link by name, it's more readable." | A bare name is not a link. `[[slug]]` is what makes the cards a graph. |
| "No next step was mentioned, so I'll log it quietly." | That is the strongest slip signal there is. Log it and say so. |
| "The pipeline should show the first stage I find." | Cards are append-only; the first is the oldest. Take the last. |
| "I know roughly what time it is." | You know the date, not the time. Run `date -u` or the entry lands at 00:00. |

## Validation

A log is correct when:

- [ ] The card exists at `crm/<kind>/<slug>.md` with the exact header block.
- [ ] Every prior entry is still present, unmodified.
- [ ] The new entry has a UTC `## YYYY-MM-DD HH:MM` heading taken from `date -u`,
      not `00:00`.
- [ ] Every field with a value is present; no empty-valued field was written.
- [ ] Every entity reference is `[[slug]]`, not a bare name.
- [ ] A deal with no `next_step` was flagged in the reply.

A pipeline summary is correct when every deal card appears exactly once, with its
**latest** stage and next step.

## Examples

**Logging a deal that moved:**

> "3G Lighting moved to procurement — Helen's championing it, next step is the
> security review by Friday."

Reads `crm/deals/3g-lighting-scheduling-mrp-opportunity.md`, appends:

```

## 2026-08-14 17:20
- name: 3G Lighting — scheduling/MRP opportunity
- company: [[3g-lighting]]
- stage: Procurement
- next_step: Security review
- next_step_owner: Helen
- champion: [[helen-li]]
```

Reply: `Logged deal '3G Lighting — scheduling/MRP opportunity' [Procurement]`

**A deal with no next step:**

Reply: `Logged deal 'Northwind expansion' [Discovery]  ⚠ no next step set — schedule one.`

**A pipeline review:**

```
Pipeline:
- 3g-lighting-scheduling-mrp-opportunity: [Procurement] next: Security review
- northwind-expansion: [Discovery] next: no next step ⚠
```
