# Threat Model — ArcRun

**Version**: 1.0
**Date**: 2026-02-21
**Scope**: ArcRun runtime agentic loop

## System Boundary

ArcRun is the runtime execution loop for LLM agents. It sits between ArcLLM
(provider calls) and ArcAgent (orchestration). ArcRun processes:

- **Inputs**: System prompts, user tasks, LLM responses (including tool calls)
- **Outputs**: LoopResult with content, events, token usage
- **Side effects**: Tool execution (arbitrary code, HTTP calls, file I/O)

## Trust Boundaries

```
+-------------------+
|   Human Operator   |  TRUSTED — provides system prompt, tool definitions
+-------------------+
         |
+-------------------+
|     ArcAgent       |  TRUSTED — orchestrates runs, manages context
+-------------------+
         |
+-------------------+
|      ArcRun        |  THIS COMPONENT — enforces execution policy
+-------------------+
    |           |
+-------+  +--------+
| ArcLLM|  | Tools   |  UNTRUSTED — LLM output is adversarial;
+-------+  +--------+    tool results are untrusted data
```

**Key principle**: LLM output and tool results are treated as untrusted input.

## OWASP LLM Top 10 (2025)

| Code | Threat | ArcRun Mitigation | Test Evidence |
|------|--------|-------------------|---------------|
| LLM01 | Prompt Injection | System prompt isolated in `role=system`; tool results stay `role=tool`; sandbox denies unauthorized tools | `security/test_prompt_injection.py`, `security/test_steering_injection.py` |
| LLM02 | Sensitive Information Disclosure | System prompt never echoed in response; no secrets in event data | `security/test_prompt_injection.py::test_system_prompt_not_in_task_response` |
| LLM03 | Supply Chain | Optional dependencies isolated (`docker>=7.0` as extra); lazy imports with clear error messages | `test_contained_execute.py::test_lazy_import_error` |
| LLM04 | Data Poisoning | Out of scope for arcrun (training data concern) | N/A |
| LLM05 | Improper Output Handling | JSON Schema validation on tool parameters; tool results wrapped in typed messages | `executor.py:43-46`, `security/test_tool_injection.py` |
| LLM06 | Excessive Agency | SandboxConfig allowlist; max_turns limit; depth/max_depth for spawn control | `security/test_spawn_depth_bomb.py`, `test_loop.py::TestSandbox` |
| LLM07 | System Prompt Leakage | No secrets stored in system prompts by design; prompts treated as potentially exfiltrable | Architecture decision |
| LLM08 | Vector/Embedding Weaknesses | Out of scope for arcrun (no vector store) | N/A |
| LLM09 | Misinformation | Out of scope for arcrun (content verification is ArcAgent concern) | N/A |
| LLM10 | Unbounded Consumption | max_turns, tool_timeout, token tracking, output truncation (max_output_bytes) | `security/test_resource_exhaustion.py` |

## OWASP Agentic Top 10 (2026)

| Code | Threat | ArcRun Mitigation | Test Evidence |
|------|--------|-------------------|---------------|
| ASI01 | Agent Goal Hijack | System prompt is set once at run start; tool results cannot modify it | `security/test_steering_injection.py` |
| ASI02 | Tool Misuse & Exploitation | Sandbox check before every tool call; JSON Schema validation; audit events on all tool calls | `security/test_tool_injection.py`, `executor.py:33-46` |
| ASI03 | Identity & Privilege Abuse | Unique run_id per execution; no credential sharing between runs | `security/test_timing_attacks.py::test_parallel_runs_unique_run_ids` |
| ASI04 | Agentic Supply Chain | Container tool uses lazy import; socket auto-detection; no auto-pull of images | `contained_execute.py`, `test_contained_execute.py` |
| ASI05 | Unexpected Code Execution | Container sandbox: read-only rootfs, no network, PID limit, seccomp, tmpfs-only writes | `contained_execute.py`, `test_contained_execute.py`, `security/test_path_traversal.py` |
| ASI06 | Memory & Context Poisoning | Events are frozen (immutable); hash chain detects tampering | `security/test_event_tampering.py` |
| ASI07 | Insecure Inter-Agent Communication | Per-run EventBus isolation; events from different run_ids cannot be replayed | `security/test_event_tampering.py::test_replay_events_from_different_run` |
| ASI08 | Cascading Failures | Shared-nothing per run; spawn depth limits; parallel spawn controls | `security/test_spawn_depth_bomb.py`, `security/test_timing_attacks.py` |
| ASI09 | Human-Agent Trust Exploitation | Out of scope for arcrun (UI/presentation concern for ArcAgent) | N/A |
| ASI10 | Rogue Agents | Audit trail via hash-chained events; verify_integrity() for post-run validation | `test_types.py::test_verify_integrity_valid_chain` |

## Attack Surface by Component

### EventBus (`events.py`)

| Attack Vector | Mitigation |
|---------------|------------|
| Event data mutation after emit | `frozen=True` dataclass + `MappingProxyType` |
| Hash chain forgery | SHA-256 with chained prev_hash; verify_chain() detects all tampering |
| Race condition on concurrent emit | `threading.Lock` in EventBus |
| Observer callback poisoning | Exceptions in on_event are caught and silenced |

### Executor (`executor.py`)

| Attack Vector | Mitigation |
|---------------|------------|
| Unauthorized tool call | Sandbox.check() before execution |
| Invalid tool parameters | JSON Schema validation via jsonschema |
| Tool execution timeout | asyncio.wait_for with configurable timeout |
| Tool execution crash | Exception caught, truncated, returned as error message |
| Tool name spoofing (unicode homoglyphs) | Exact string match in sandbox allowlist and registry |

### Container Sandbox (`contained_execute.py`)

| Attack Vector | Mitigation |
|---------------|------------|
| Network exfiltration | network_disabled=True (default) |
| Filesystem escape | read_only=True rootfs + tmpfs on /tmp only |
| Resource exhaustion (CPU) | cpu_quota/cpu_period limits |
| Resource exhaustion (memory) | mem_limit=256m (default) |
| Process bomb | pids_limit=64 (default) |
| Privilege escalation | security_opt=["no-new-privileges"], seccomp=unconfined only if not available |
| Path traversal | Code injected via tar to /tmp; rootfs is read-only |
| Container escape | Standard Docker/Podman isolation; no --privileged |

### Spawn System (`spawn.py`)

| Attack Vector | Mitigation |
|---------------|------------|
| Infinite recursive spawn | depth/max_depth enforcement |
| Spawn flood (DoS) | Parallel tool execution with turn limits |
| Depth field manipulation by model | Depth parameter comes from runtime, not model output |
| Tool list expansion in child | Only tools from parent's tool list available |

### Strategy Layer (`strategies/react.py`)

| Attack Vector | Mitigation |
|---------------|------------|
| Infinite loop (model never stops) | max_turns hard limit |
| Steer/cancel race conditions | Queue-based steering; cancel_event checked per iteration |
| Token exhaustion | Token tracking in state; budget enforcement possible at ArcAgent layer |

## Residual Risks

| Risk | Severity | Mitigation Path |
|------|----------|-----------------|
| In-memory events lost on crash | Medium | ArcAgent layer should persist events to durable store |
| No mTLS between arcrun and arcllm | Low | Same-process communication; mTLS at ArcAgent NATS layer |
| Container image trust | Medium | No auto-pull (D-014); caller responsible for image provenance |
| LLM model trust | High | Out of scope — ArcLLM/ArcAgent concern for model validation |
