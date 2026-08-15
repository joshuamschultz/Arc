---
name: crm
description: "Maintain the revenue book of business — contacts, companies, deals, meetings, commitments — as append-only cards under workspace/crm/, and read the open pipeline back. Card writing runs through scripts/crm.py so slugs, timestamps, links and append semantics are exact; this skill supplies the judgement about what to log. TRIGGER: any mention of a person, account, deal, meeting outcome, or promise made either way ('log this', 'we agreed to', 'they moved to procurement'), and any request for the pipeline. SKIP: judging whether a deal is healthy (use deal-review), preparing for a specific call (use pre-call-brief), or listing what is overdue across accounts (use follow-up-sweep) — those READ these cards, they do not write them."
version: 3.0.0
---

## Resources

(auto-filled by the loader)

## Contract

Every CRM write goes through `scripts/crm.py`. Given something worth recording:

1. The right entity kind is chosen and the values are passed as flags.
2. The script writes the card; it is never hand-written with `write` or `edit`.
3. The reply names what was logged, in one line.
4. A deal logged with no next step is flagged out loud.
5. Nothing else in the workspace is modified.

## Knowledge

**Why a script and not prose.** Slugging, the UTC timestamp, the card header, the
append-never-rewrite, which fields render as `[[slug]]`, and which value counts as
current are all mechanical — and each is a thing that, re-derived per call, is
eventually derived differently. A slug spelled two ways forks one account into two
cards that each tell half the truth; a timestamp written from memory lands at
`00:00`; a card "tidied" on the way past loses the history the pipeline is read
from. The script settles all of it, the same way every time. What is left here is
the part that actually needs judgement: which entity this is, whether the stage
really moved, and what is worth recording at all.

**The judgement the script cannot make:**

- **Is this the same entity, or a new one?** The script slugs whatever name it is
  given. Give it the name already on the card — check with
  `ls crm/contacts/` or `grep -ril "<name>" crm/` before inventing a variant.
- **Did the stage actually move,** or is it being restated? Log the move; do not
  re-log an unchanged stage just because it came up.
- **Never invent a stage, value, or date that was not said.** Omit the flag
  entirely — an empty field is honest, a guessed one becomes someone's forecast.
- **No scheduled next step is the strongest slip predictor there is.** The script
  flags it; say it out loud rather than burying it.
- A commitment that names a deal is logged twice on purpose — once to the
  commitments log, once to that deal — so run the script for each.

## Steps

**To log something** (run from the workspace, which is where the shell already is):

```bash
python3 capabilities/skills/crm/scripts/crm.py log deal \
  --name "3G Lighting — scheduling/MRP opportunity" \
  --company "3G Lighting" --stage "Procurement" \
  --next-step "Security review" --champion "Helen Li"
```

Kinds and their flags — pass only what was actually said:

| Kind | Flags |
|---|---|
| `contact` | `--name` (required), `--role`, `--company`… see `--help` |
| `company` | `--name` (required), `--industry` |
| `deal` | `--name` (required), `--company`, `--stage`, `--value`, `--next-step`, `--next-step-owner`, `--champion` |
| `meeting` | `--title` (required), `--attendees`, `--deal`, `--follow-ups` |
| `commitment` | `--commitment` (required), `--owner`, `--due`, `--deal` |

Every kind also takes `--note` for free-text context. Run
`python3 capabilities/skills/crm/scripts/crm.py log <kind> --help` when unsure —
the script is the schema, and a mistyped flag is refused rather than written.

**To read the pipeline:**

```bash
python3 capabilities/skills/crm/scripts/crm.py pipeline
```

Report its output as-is. It already reports each deal's latest stage and next
step, and marks deals with no next step.

## Steps — before logging a name you have not seen

1. `grep -ril "<name>" crm/` to find an existing card.
2. If one exists, pass the name exactly as that card records it.
3. Only if nothing matches is this a new entity.

## Red Flags & Rationalizations

| Thought | Reality |
|---|---|
| "I'll just write the card with `write`, it's one file." | Then the slug, timestamp and append are yours to get right, every time, forever. Use the script. |
| "The name is close enough to the existing card." | Close enough forks the account. `grep` first, then pass the recorded name. |
| "They didn't give a stage, I'll infer one." | An invented stage becomes someone's forecast. Omit the flag. |
| "No next step was mentioned, so I'll log it quietly." | It is the strongest slip signal there is. The script flags it; repeat the flag. |
| "The commitment mentions a deal, one run covers it." | Two runs: the commitments log and the deal card. |
| "I'll fix the card format by hand afterwards." | Hand edits are exactly what the script exists to prevent. |

## Validation

A log is correct when:

- [ ] It was written by `scripts/crm.py`, not by `write` or `edit`.
- [ ] The name passed matches the existing card's name when one existed.
- [ ] Only fields that were actually stated were passed.
- [ ] A deal with no next step was flagged in the reply.
- [ ] A commitment naming a deal was logged to both.

A pipeline report is correct when it is the script's output, unedited.

## Examples

**A deal that moved:**

> "3G Lighting moved to procurement — Helen's championing it, next step is the
> security review."

```bash
python3 capabilities/skills/crm/scripts/crm.py log deal \
  --name "3G Lighting — scheduling/MRP opportunity" --company "3G Lighting" \
  --stage "Procurement" --next-step "Security review" --champion "Helen Li"
```

Reply: `Logged deal '3G Lighting — scheduling/MRP opportunity' [Procurement]`

**A deal with nothing scheduled:**

Reply: `Logged deal 'Northwind expansion' [Discovery] ⚠ no next step set — schedule one.`

**A pipeline review:**

```
Pipeline:
- 3g-lighting-scheduling-mrp-opportunity: [Procurement] next: Security review
- northwind-expansion: [Discovery] next: no next step ⚠
```
