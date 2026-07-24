---
name: contained_exec_guidance
description: Guidance for the contained_execute_python container-isolated code tool.
tunable: true
---
## Isolated Code Execution (contained_execute_python)
You have access to a container-isolated Python execution environment.
It runs with no network access, a read-only filesystem, and strict
memory/CPU/PID limits. Use it for the same scenarios as execute_python
but when stronger isolation is required — untrusted input processing,
resource-intensive computation, or when the execution environment must
not affect the host.
