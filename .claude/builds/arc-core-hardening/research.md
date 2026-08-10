# arc-core-hardening — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-291–D-312 (22 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## Feature: arc-core-hardening

**Date:** 2026-04-17
**Scope:** Stability, integration gaps, tool policy pipeline, execution engine upgrade, heartbeat/cron system
**Status:** Deepened (research-enriched)

---

#### Architecture

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|

##### Research Insights: Decision 1 — Parallel Tool Execution

**Failure handling:** Use `asyncio.gather(return_exceptions=True)` — partial success is meaningful for tool batches. A failed `read_file` shouldn't abort a concurrent `web_search`. Inspect results post-gather, emit structured error events per failed tool. Do NOT use `TaskGroup` (fail-fast semantics wrong for tool batches; also had deadlock edge cases in Python 3.11).

**Concurrency limit:** Add `asyncio.Semaphore(max_parallel_tools)` with configurable default of 10. Unbounded `gather()` risks memory exhaustion (N tools x avg output size) and fd exhaustion (default Linux ulimit 1024). Semaphore releases slots as tasks complete, maintaining continuous throughput — better than chunked gather.

**Read-write classification:** Classify tools as read-only or state-modifying at registration time. Run batch in parallel ONLY if ALL tools are read-only. Any write in the batch forces the entire batch sequential. This is the Anthropic/Claude Code pattern and eliminates filesystem races without per-file locking.

**Implicit dependency detection:** Parameter-based heuristic: if tool call A produces a file path that appears as an argument in tool call B in the same batch, treat as dependent and execute sequentially. Catches the write-then-read pattern cheaply without full DAG planning (LLMCompiler).

**Audit ordering:** Assign monotonically incrementing sequence number at dispatch time (before gather). Record wall-clock at dispatch AND completion separately. Events get `{seq, tool_id, dispatch_ts, complete_ts, status}`. Sequence number establishes submission order; timestamps provide duration.

**FIPS/air-gapped:** Connection pooling via httpx client reuse. Semaphore on HTTPS-touching tools (2-4 concurrent on FIPS-constrained 2-vCPU VMs). FIPS TLS handshakes can reach 5s under contention.

**Scalability ceiling:** ~2KB per coroutine. Practical limit is rate limits on downstream services, not asyncio. Cap at 10-20 concurrent tools per turn via semaphore.

---

##### Research Insights: Decision 4 — Tool Policy Pipeline

**Pattern confirmed:** First-DENY-wins is correct — every major production system (AWS IAM, K8s RBAC, Istio, Envoy, OPA) uses this. First-ALLOW-wins is catastrophically wrong for security pipelines.

**Fail-closed is non-negotiable:** AuthZed's analysis: "a fail-open state can inadvertently grant access to unauthorized users during unexpected failures." Any exception during evaluation = DENY. Never propagate exceptions as allow.

**Performance target:** Sub-1ms per evaluation. OPA benchmarks: 40-50us with rule indexing. Short-circuit on first DENY. For 5 layers sequentially: budget ~5ms total, but most requests DENY in first 1-2 layers.

**Optimizations:**
- Short-circuit evaluation (return immediately on first DENY)
- Index rules by tool name for O(1) lookup, not O(n) iteration
- Decision caching: LRU cache keyed on `(agent_did, tool_name, classification)` with 30-60s TTL
- Load all reference data into memory at startup (never external calls during evaluation)

**Structured deny reasons MUST answer 3 questions:**
1. Which layer denied?
2. Which rule matched?
3. What input values triggered it?

Example: `"Tool requires SECRET clearance; agent has FOUO"` — not `"Access denied"`.

**Classification filtering (Bell-LaPadula):**
- No read up: agent can't call tools returning data above its clearance
- No write down: agent with classified context can't call tools that write to unclassified systems
- Classification must propagate through call chains (Agent A -> Agent B -> tool)

**Dynamic tool registration = privileged action:** Must be governed by Global layer. Newly registered tools classified before callable. Scoped to creating session (not globally visible).

**Dry-run/shadow mode:** Deploy new policies in audit-only mode before enforcing. Log denials without blocking. Essential for policy hot-reload safety.

**Air-gapped:** Signed policy bundles distributed like software releases. Local bundle fallback with max-age check. If bundle too old and server unreachable, enter restricted mode (deny all except hardcoded safe set).

---

##### Research Insights: Decision 5 — ProactiveEngine

**Timer architecture:** Single loop with min-heap priority queue (Celery Beat pattern). One asyncio task, priority queue sorted by `next_run`, sleep until earliest entry. Polling granularity: 1-5 seconds.

**Drift prevention:** Compute `next_run = last_actual_run + interval`, NEVER `next_run = now + interval`. The latter accumulates drift. Apply small negative adjustment (-0.010s) per cycle for scheduling overhead (Celery pattern). Add jitter parameter for multi-instance herd prevention.

**Clock source:** Use `time.monotonic()` (maps to `CLOCK_BOOTTIME` on Linux since Python 3.x) for interval calculations. Wall clock ONLY for "is it within active hours?" checks. `CLOCK_MONOTONIC` stops advancing during VM suspend — `CLOCK_BOOTTIME` includes suspend time. Add clock warp detection: compare `time.time()` delta against `time.monotonic()` delta per tick; warn if divergence > 5s.

**Heartbeat isolation (CRITICAL):** Heartbeat outputs MUST NOT enter the agent's main context window. Run heartbeat as a stateless side-channel call with its own minimal context. Different model outputs in main context cause behavioral inconsistencies.

**Cheap model for heartbeat:** Restrict decision boundary to `{idle, not_idle}`. Never `{idle, act_on_X, act_on_Y}`. Anything other than clear idle signal escalates to full model.

**Circuit breaker state machine (Resilience4j pattern):**
- CLOSED: normal, failures tracked in sliding window
- OPEN: disabled, rejects immediately, waits `waitDuration`
- HALF_OPEN: N probe executions allowed, success -> CLOSED, failure -> OPEN
- Exponential backoff on wait: `min(60 * 2^open_count, 1800)` seconds
- `auto_recovery_enabled = true` (moves OPEN -> HALF_OPEN automatically)
- Always expose `force_close()` and `force_open()` for manual override

**Concurrency policy:** If previous execution hasn't completed, skip new invocation and record miss. Prevents unbounded parallel agent runs. This is Kubernetes CronJob `concurrencyPolicy: Forbid`.

**Wake events:** Idempotent — compare `event.timestamp` against `last_wake_handled`. Discard stale wakes.

**Active hours / timezone:**
- Store UTC internally, IANA timezone for user-facing config (e.g., `America/Chicago`)
- Convert at schedule creation, never at evaluation time
- Overnight windows (22:00-06:00): detect `end < start`, handle as `now >= start OR now < end`
- DST: skip non-existent spring-forward times, don't double-execute on fall-back

**Multi-instance (10-20 agents):** Leader election via Kubernetes Lease or Redis. Only leader runs ProactiveEngine. If leader dies, lease expires and new instance acquires. Simpler and safer than per-schedule distributed locking. All scheduled actions MUST be idempotent (at-least-once semantics).

---

#### Data Model

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|

##### Research Insights: Decision 13 — Dynamic Tool Format

**Decorator pattern:** Return original function unchanged (transparent). Store metadata separately from callable. Lock down registration after startup to prevent late/concurrent mutation. Use outermost decorator position for `@tool`.

**Schema from type hints, not manual:** Derive JSON Schema from Python type hints via Pydantic `validate_call`. AI writes typed functions; framework generates schema. This is the FastMCP, LangChain, and OpenAI Agents SDK pattern. Never let AI write schema manually alongside untyped functions.

**importlib isolation:** Do NOT register in `sys.modules`. Use unique module names: `f"_agent_tools.{name}_{hash(path)}"`. Use `Path.resolve()` before `spec_from_file_location`. Create fresh module object each time (no `reload()`).

**Name collision policy:** Namespace prefix agent-created tools: `agent.{session_id}.{name}`. Reserve `builtin.*` namespace. On collision: `"warn"` mode replaces with warning (FastMCP pattern). Configurable: `"error"`, `"replace"`, `"warn"`, `"ignore"`.

**Version conflict during execution:** Copy tool reference at dispatch time. Registry update takes effect for next call, not in-flight calls. Use asyncio.Lock for concurrent registration.

**Signature validation:** AST pre-validation checks function params match schema keys before loading. Pydantic `validate_call` wrapping at registration for runtime type coercion. Fail at registration, not at call time.

**Error handling:** Tool errors surfaced with source file + traceback (Python `linecache` auto-caches for real .py files). Timeout via `asyncio.wait_for(coro, timeout)` for async tools. For sync tools that might hang, use subprocess (killable) not threads (not killable).

---

#### API Design

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|

---

#### Observability

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|

##### Research Insights: Decision 18 — Telemetry

**Policy evaluation telemetry must include:**
- `request_id` (correlate with agent trace)
- `session_id` (correlate with full session)
- `layer` (which policy layer evaluated)
- `policy_version` (which bundle was active)
- `decision` (ALLOW/DENY/ERROR)
- `matched_rule` (specific rule ID)
- `evaluation_time_us` (microseconds)
- `input_hash` (for cache validation)

**Metrics to instrument:**
- Denial rate by layer (spike = policy regression)
- Evaluation latency by layer (p50/p95/p99)
- Exception rate (any non-zero = policy bug)
- Cache hit rate
- Circuit breaker state per schedule
- Heartbeat tick rate and silent suppression count

---

#### Security

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|

##### Research Insights: Decision 19 — CRITICAL SECURITY GAPS IDENTIFIED

**AST scanning is necessary but insufficient.** RestrictedPython has had 3 CVEs (2023-37271, 2025-22153, 2024-47532) demonstrating bypass via generator frame traversal, try/except* confusion, and AttributeError.obj leakage.

**Immediate additions to blocked list (beyond os, subprocess, socket, etc.):**

| Must Block | Why |
|---|---|
| `ctypes` (CDLL, cdll, windll) | CVE-2025-68668: `CDLL(None).system("cmd")` bypasses ALL Python-level restrictions via libc FFI |
| `sys` (sys.modules) | Pre-loaded modules (including `os`) accessible without import via `sys.modules['os']` |
| `.gi_frame`, `.f_back`, `.f_builtins`, `.f_globals` | CVE-2023-37271: generator frame traversal reaches unrestricted `__import__` |
| `compile()` + `eval()` combination | Bypasses eval's single-expression restriction |
| `pickle` / `__reduce__` | Arbitrary code execution on unpickling |
| `__class__.__base__.__subclasses__()` | Class hierarchy traversal reaches importers |
| `getattr(__builtins__, ...)` | Dynamic attribute access to blocked builtins |
| `string.Formatter` | Format string attribute traversal leaks globals |
| Source encoding declarations | `# -*- coding: utf-7 -*-` codec attacks occur BEFORE AST parsing |

**NETWORK EGRESS IS UNADDRESSED (NEW REQUIREMENT):**
Even a perfectly process-isolated tool can exfiltrate data via outbound HTTP. Every tier needs explicit egress allowlist (deny-by-default outbound, approve specific endpoints). This is the `ToolContext.http` proxy — it must be the ONLY network path, and it must be logged.

**Recommended layered defense for tools:**
1. AST validation (blocks unsophisticated attempts)
2. Restricted builtins (scrubbed `__builtins__` dict)
3. Blocked attribute access (gi_frame, f_back, etc.)
4. Network egress proxy (deny-by-default, logged)
5. Policy pipeline (5-layer, execution-time)
6. Optional: seccomp-bpf syscall allowlist via subprocess (blocks everything AST misses)

**Non-compositional safety (arXiv:2603.15973):** Two individually-safe tools can compose to enable forbidden capabilities (e.g., "read file" + "send HTTP" = exfiltration). Consider capability inventory at deployment: what syscalls, network endpoints, and file paths does each tool require? Do combinations create dangerous conjunctions?

**Federal posture is defensible under NIST 800-53:**
- SI-7(15): Dynamic code fails cryptographic pre-installation authentication
- CM-5: Agent-generated code bypasses change control
- CM-8: Dynamic tools create untracked system components
- Frame as compliance requirement, not product limitation

**OWASP Agentic Top 10 (2026) relevant items:**
- ASI01: Created tool becomes vector for goal hijack
- ASI02: Agents chain self-created tools in unexpected sequences
- ASI04: Dynamically loaded extensions with compromised MODULE.yaml
- ASI05: Agent-generated code treated as trusted
- ASI08: Extensions subscribing to events trigger cascading failures

---

#### Testing

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|

##### Research Insights: Decision 21 — Adversarial Test Cases

Based on research, the adversarial suite MUST include:

**Import bypass tests:**
- `__import__('os')` via builtins
- `sys.modules['os']` access
- Class hierarchy traversal (`__subclasses__()`)
- Generator frame traversal (gi_frame.f_back)
- ctypes FFI (`CDLL(None).system()`)
- Codec attack (utf-7 encoding)
- Format string globals leakage
- compile() + eval() combination
- pickle __reduce__ exploitation

**Capability composition tests:**
- read_file + http_request = data exfiltration
- create_tool + reload = privilege escalation
- bash + write_file = persistent backdoor

**Policy pipeline tests:**
- Concurrent policy evaluation under load
- Exception in middle layer (verify fail-closed)
- Dynamic tool registration during policy evaluation
- Classification downgrade attempt (write-down)
- Stale policy bundle detection

---

#### CLI

| # | Decision | Choice | Priority | Tier Variation |
|---|----------|--------|----------|----------------|

---

#### Auto-Applied Federal Mandates

| Mandate | Citation | Applied To |
|---------|----------|------------|
| Every policy evaluation audit-logged | NIST 800-53 AU-2 | Tool policy pipeline |
| Completion events audit-logged | NIST 800-53 AU-2 | task_complete tool |
| Budget tracking mandatory (federal) | OMB A-123, FITARA | Turn/cost limits |
| Self-modification actions audit-logged | NIST 800-53 AU-2 | create_skill, create_tool, create_extension |
| Proactive execution audit-logged | NIST 800-53 AU-2 | ProactiveEngine (trigger type tagged) |
| Audit log retention minimum 1 year | NIST 800-53 AU-11 | All audit logs |
| Agent-created code cannot bypass audit | NIST 800-53 AU-9 | Dynamic tool sandbox |
| Schedule changes audit-logged | NIST 800-53 AU-2 | ProactiveEngine |
| Dynamic code DENIED in federal tier | NIST 800-53 SI-7(15), CM-5, CM-8 | Tools + extensions |
| Network egress deny-by-default | NIST 800-53 SC-7 | All tiers |

---

#### Known Bug Fixes (Not Decisions -- Will Be Fixed)

1. **ArcLLM bridge not wired:** `create_arcllm_bridge()` defined but `on_event` never passed to `load_model()`
2. **ui_reporter missing MODULE.yaml:** Cannot be loaded by convention-based ModuleLoader
3. **REPL commands non-functional:** `/sandbox` and `/strategy` in `arc agent chat` are display-only
4. **httpx client leak:** `ArcAgent.shutdown()` doesn't call `model.close()`
5. **messaging byte_pos=0:** `svc.ack()` always passes `byte_pos=0`, breaking seek optimization
6. **Hardcoded constants:** `_CHECK_CIRCUIT_BREAKER_THRESHOLD`, `_MAX_STEERING_MESSAGE_LEN`, WebSocket URL, OTEL endpoint -- move to config

---

#### New Files Summary

| File | Package | Purpose |
|------|---------|---------|
| `core/tool_policy.py` | arcagent | Policy pipeline types, layers, pipeline class |
| `modules/proactive/engine.py` | arcagent | Unified ProactiveEngine (replaces pulse + scheduler) |
| `modules/proactive/MODULE.yaml` | arcagent | Module metadata |
| `tools/skill_tools.py` | arcagent | create_skill, improve_skill |
| `tools/tool_tools.py` | arcagent | create_tool, list_artifacts, reload_artifacts |
| `tools/extension_tools.py` | arcagent | create_extension |
| `tools/schedule_tools.py` | arcagent | create/list/pause/resume/delete schedule |
| `tools/completion.py` | arcagent | task_complete tool |
| `tools/_decorator.py` | arcagent | @tool decorator for dynamic tools (Pydantic schema inference) |
| `tools/_dynamic_loader.py` | arcagent | Dynamic tool loading with AST validation + restricted builtins |
| `builtins/task_complete.py` | arcrun | task_complete built-in |

#### Modified Files Summary

| File | Change |
|------|--------|
| `arcagent/core/agent.py` | Wire LLM bridge, register new tools, model.close() in shutdown |
| `arcagent/core/tool_registry.py` | Replace `_check_policy()` with pipeline, move to execution-time, add read-write classification |
| `arcagent/utils/__init__.py` | Add `on_event` parameter to `load_eval_model()` |
| `arcagent/modules/ui_reporter/MODULE.yaml` | NEW -- enable convention loading |
| `arcagent/modules/messaging/tools.py` | Fix byte_pos in ack() |
| `arcagent/modules/pulse/` | Replaced by proactive/ |
| `arcagent/modules/scheduler/` | Replaced by proactive/ |
| `arccli/agent.py` | Fix /sandbox and /strategy REPL, add new CLI commands |
| `arcrun/strategies/react.py` | Parallel tool execution with semaphore + read-write classification |
| `arcrun/loop.py` | task_complete handling, max_turns enforcement |
| Various configs | Move hardcoded constants to TOML |

---

#### Research Sources

**Parallel Execution:** asyncio.gather patterns (SuperFastPython), LLMCompiler ICML 2024 (arXiv:2312.04511), Lamport timestamps, TLS handshake latency under FIPS
**Policy Pipeline:** AWS IAM evaluation logic, OPA performance docs, Goldman Sachs OPA at scale, AuthZed fail-open analysis, NIST 800-53 AC-3, Bell-LaPadula model, Istio dry-run mode
**Self-Modification Security:** RestrictedPython CVEs (2023-37271, 2025-22153, 2024-47532), n8n Pyodide escape (CVE-2025-68668), OWASP Agentic Top 10 2026, Safety non-compositionality (arXiv:2603.15973), secimport eBPF, NIST SI-7/CM-5/CM-8
**ProactiveEngine:** Celery Beat architecture, Resilience4j circuit breaker, ROS2 watchdog patterns, TigerBeetle clock research, EventBridge scheduler, Kubernetes leader election
**Dynamic Tools:** FastMCP tool registration, Pydantic validate_call, importlib isolation patterns, pluggy namespace isolation

---

---
