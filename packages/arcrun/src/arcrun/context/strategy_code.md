---
name: strategy_code
description: Code-exec strategy prompt guidance.
tunable: true
---
## Code-First Execution
Solve this task by writing and running code, not by working it out in your head. Code is exact where reasoning drifts: counting, arithmetic, parsing, sorting, comparing, and anything repeated over many items.

GUIDELINES:
- Split the work into small scripts, one sub-problem each, and check each result before building on it
- Put the numbers and records into the code itself; never retype a computed value by hand
- If a result looks wrong, inspect the data with a smaller script before changing your approach
- Use other tools, not code, for external APIs, user confirmation, and security-sensitive operations
- Finish with a plain-language answer that states the result, not the code that produced it
