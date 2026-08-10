# Quality checklist — open-loop-sweep

- [ ] Promises are split into "you owe" and "owed to you".
- [ ] No item appears in both directions.
- [ ] Items that appear settled were confirmed with the operator before being closed.
- [ ] Confirmed items were written back with `pa_log_commitment` status=done.
- [ ] Closed items are stated once and dropped, not retained "for the record".
- [ ] Undated commitments are surfaced in their own section.
- [ ] Stale items state their age, and the staleness bar used is stated.
- [ ] Every open item has one next action, or an explicit "waiting on <person>".
- [ ] Nothing appears that is absent from the cards.
