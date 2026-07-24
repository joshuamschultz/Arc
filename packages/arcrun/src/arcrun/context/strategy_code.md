---
name: strategy_code
description: Code-exec strategy prompt guidance.
tunable: true
---
## Code Execution Strategy
You can write and execute Python code to solve tasks. Prefer code when the problem involves computation, data processing, or logic that is more naturally expressed as a script than as tool calls.

GUIDELINES:
- Write focused scripts (20-50 lines) solving one sub-problem at a time
- You receive {stdout, stderr, exit_code, duration_ms} after each execution
- Each execution is stateless — variables do NOT persist between calls
- If code fails, examine the error and fix your approach
- After 3 failures on the same approach, try a fundamentally different method
- Use code for: computation, data processing, logic, file operations
- Use other tools for: external APIs, user confirmation, security-sensitive operations
