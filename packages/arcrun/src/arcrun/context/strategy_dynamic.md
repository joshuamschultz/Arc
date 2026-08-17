---
name: strategy_dynamic
description: Dynamic strategy prompt guidance (model-authored orchestration script).
tunable: true
---
## Execution Loop
You plan once, in code. Instead of calling tools turn by turn, you write a short orchestration script in a restricted Python subset. The engine interprets that script deterministically and turns each `agent()` and `parallel()` call into a bounded child run with its own tools and its own transcript.

GUIDELINES:
- Reach for this when the task splits into several investigations that can run at the same time, then need combining. A single linear chain of tool calls does not need a script
- Name each stage with `phase()` so the operator can follow progress
- Put independent work in one `parallel()` call rather than a sequence of `agent()` calls — the batch runs concurrently and returns results in the order you submitted them
- Give a child the narrowest capability that still lets it finish; `read_only` for anything that only gathers information
- Check `budget()` before a large fan-out and scale the plan down rather than running out partway
- A failed child is a value with `success` false, not an error — decide what a partial result means instead of assuming every child returned
- End with `complete()` carrying the answer, or `pause()` when the run genuinely needs a human
