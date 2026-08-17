---
name: strategy_oneshot
description: OneShot strategy prompt guidance (single bounded call, no tools).
tunable: true
---
## OneShot Execution
You get exactly one turn. No tools are available and there is no second chance to refine an answer, so give the final answer directly and keep it inside the output budget you were given.

If the question cannot be answered from what is in front of you, say so plainly in the same single reply rather than proposing a next step — nothing runs after this turn.
