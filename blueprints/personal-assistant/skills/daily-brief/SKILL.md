---
name: daily-brief
description: "Deliver the short morning brief — what is due or breaking today, what is going past its date, and who has gone quiet — capped at what actually needs attention. TRIGGER: the morning schedule, or 'what do I need to know today'. SKIP: a full sweep of every open promise (use open-loop-sweep) or prepping for one specific person (use person-brief)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the brief must clear before it is delivered.
- `examples/tuesday-brief.md` — a worked brief on a quiet day, showing the short form.

## Contract
Given the cards in `workspace/personal/` and memory, produce a brief such that:
1. It leads with anything due or breaking **today** — nothing else goes above that.
2. Commitments going past their date are named with who is owed and how late.
3. It is capped at **five items**. A sixth item means something was not prioritized.
4. On a quiet day it says so in one line and stops. It does not pad to look useful.
5. Facts and inferences are labelled differently: "you told me" versus "it looks like".
6. Nothing is invented — a name, date, or amount absent from the cards does not appear.
Nothing is modified; this reads memory.

## Knowledge
- People do not want a daily digest. They want the one thing that matters, on the day it
  matters. The value of the brief is what it leaves out.
- A brief that arrives every morning at the same length regardless of the day teaches the
  reader to skim it, and then it stops working entirely.
- Attention is the scarce thing. Anything that can wait should wait.
- A person going quiet longer than usual is a real signal, and it is one only durable memory
  can produce — surface it when the gap is unusual for that relationship.

## Steps
1. Read open loops via `pa_open_loops`. Gate: state the counts.
2. Identify anything due or breaking today. Gate: list it, or state "nothing due today".
3. Identify commitments past their date. Gate: name who is owed and how many days late.
4. Check for relationships gone quiet relative to their normal rhythm. Gate: name any, or state none.
5. Cut to five items, ordered by what breaks first. Gate: confirm five or fewer.
6. Label each item fact or inference. Gate: run the checklist, report pass/fail.

## Output
Short markdown: `# <weekday>, <date>`, then only the sections that have content — **Today**,
**Going past its date**, **Gone quiet**. One line per item. On a quiet day: one sentence.
No preamble, no sign-off, no "let me know if you need anything".

## Red Flags & Rationalizations
- The brief is the same length every day — it is a template, not a brief.
- Nine items are listed — nothing was prioritized, so nothing will be read.
- An inference is stated as a fact ("Sam is annoyed" rather than "Sam has not replied in 9 days").
- The brief ends with an offer to help. That is padding.

| Rationalization | Rebuttal |
| "I'll include it just in case." | Just-in-case items are why briefs stop being read. Cut it. |
| "There's nothing today, but here's a summary." | Then say there is nothing today. One line. |
| "Six items all genuinely matter." | Rank them. The sixth waits until tomorrow. |
| "They'd want the context too." | They want the item. Context is one question away. |

## Validation
Pass when the checklist in `validation/quality-checklist.md` is all checked and the brief
could be read in under thirty seconds. Claims of "done" require the rendered brief pasted,
not described.

## Examples
- `examples/tuesday-brief.md` — a quiet day handled in three lines instead of padded.
