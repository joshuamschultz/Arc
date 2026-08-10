---
name: process-defect-report
description: "Turn repeated slips on the same step into a named process defect with one concrete change to try, rather than another round of chasing individuals. TRIGGER: the same thing keeps going wrong, or the slip count crosses the repeat threshold. SKIP: chasing today's outstanding items (use owner-sweep) or running the weekly review (use cadence-review)."
version: 1.0.0
---

## Files
- `validation/quality-checklist.md` — the bar the report must clear before it is delivered.
- `examples/handoff-defect.md` — a worked report turning three slips into one structural fix.

## Contract
Given the slip history in `workspace/ops/slips/`, produce a report such that:
1. The defect is stated as a **property of the process**, never as a property of a person.
2. The evidence is the specific slip instances: dates, the commitment, and the step.
3. The step where work actually stalls is identified — usually a handoff, not a task.
4. Exactly ONE change is proposed. Not a list of improvements.
5. The proposed change names what would be observed if it worked, and by when.
6. Capacity problems are distinguished from process problems and labelled as such.
Findings are written back with `ops_log_process` when the process definition is corrected.

## Knowledge
- Three misses on the same step is not bad luck. It is a defect, and treating the third
  instance as an incident guarantees a fourth.
- Most operational defects live at handoffs, not inside tasks. The person doing the work is
  rarely the constraint; the moment work changes hands usually is.
- A defect caused by one person carrying too much is a capacity problem. Proposing a
  process change for a capacity problem fixes nothing and adds ceremony.
- One change at a time. Two simultaneous changes make the result unattributable, and the
  next report cannot tell which one worked.
- Adding a checklist is the default non-answer. Prefer removing a step, moving a decision
  earlier, or making an owner explicit.

## Steps
1. Run `ops_slip_patterns` to get the repeat counts. Gate: state the threshold and the counts.
2. Pull the individual slip instances behind the count. Gate: list dates, commitments, and steps.
3. Locate where work actually stalls — name the handoff or step. Gate: state it precisely.
4. Test the capacity hypothesis: is one person the common factor? Gate: state process or capacity.
5. State the defect as a property of the process. Gate: re-read it — if a name appears, rewrite.
6. Propose exactly one change, with its observable and a date. Gate: confirm one, not several.
7. Run the quality checklist. Gate: report pass/fail.

## Output
A markdown report: `# Process defect — <process>/<step>`, then sections **The pattern**
(instances with dates), **Where it stalls**, **The defect** (stated structurally),
**Process or capacity**, **The one change**, **What we should observe, and by when**.

## Red Flags & Rationalizations
- The report names a person as the defect — that is a performance conversation, not an ops report.
- Four improvements are proposed — nothing will be attributable next month.
- The change is "add a checklist" or "communicate better" with no structural edit.
- The evidence is a count with no instances behind it.

| Rationalization | Rebuttal |
| "It really is that one person." | Then it is a capacity or role problem. Say that, and do not dress it as process. |
| "Let's fix all three while we're here." | Then next month you cannot tell which fix worked. One change. |
| "A reminder will solve it." | Reminders decay. Change the structure, not the volume. |
| "We just need to be more disciplined." | Discipline is not a mechanism. Name the step and change it. |

## Validation
Pass when the checklist in `validation/quality-checklist.md` is all checked, the defect
statement survives a read with no person named, and exactly one change is proposed with a
dated observable. Claims of "done" require the rendered report pasted, not described.

## Examples
- `examples/handoff-defect.md` — three slips traced to one handoff, fixed by moving a decision earlier.
