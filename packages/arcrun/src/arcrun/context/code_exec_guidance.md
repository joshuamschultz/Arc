---
name: code_exec_guidance
description: Guidance for the execute_python sandboxed code tool.
tunable: true
---
## Code Execution (execute_python)
You have access to a sandboxed Python execution environment. Use it when
the problem is more naturally solved by writing code than by calling
predefined tools.

Prefer execute_python when:
- The task involves computation, math, or data transformation
- You need to process structured data (parse JSON, CSV, etc.)
- Logic is complex enough that reasoning alone is error-prone
- You need to verify a hypothesis empirically

Prefer other tools when:
- A dedicated tool already handles the operation (file read/write, search)
- The task requires external API access or credentials
- The operation is security-sensitive or irreversible
