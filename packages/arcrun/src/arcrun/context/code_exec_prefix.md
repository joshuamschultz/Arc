---
name: code_exec_prefix
description: How to run code, prepended to the system prompt by the code strategy.
tunable: true
---
Run code with the code-execution tool in your tool list: execute_python when you have it; otherwise a shell tool, running python3 on a script you pass in.

HOW EXECUTION WORKS:
- Each run is stateless: variables do NOT persist between calls, so print or write to a file anything you need again
- Read the output, the errors and the exit code of every run before you start the next one
- If a run fails, fix the cause the error names rather than retrying the same code
- After 3 failures on the same approach, try a fundamentally different method
