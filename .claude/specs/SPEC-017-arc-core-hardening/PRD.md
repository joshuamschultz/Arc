# PRD: Arc Core Hardening

**Spec:** SPEC-017
**Type:** integration
**Status:** pending-approval
**Coding Identity:** principled-coder

Every requirement is filtered through the four pillars in order: **Simplicity → Modularity → Security → Scalability**. Every acceptance criterion is tagged with its governing pillar(s).

---

## 1. Problem Statement

Arc has reached functional feature completeness across its packages, but a hardening pass is required before it is production-ready for federal on-prem deployment (DOE, National Labs, NASA). Five problem areas block that step:

1. **Integration gaps** — six known bugs break end-to-end flows (LLM bridge event plumbing, module loader, REPL commands, resource cleanup, message ack seek, hardcoded constants).
2. **Tool policy is spread across the tool registry** — ad-hoc `_check_policy()` at registration-time only, no layered evaluation, no audit of deny reasons, not tier-aware.
3. **Runtime executes tools sequentially** — no parallelism despite 80%+ of real tool batches being read-only (read_file, grep, ls, find).
4. **Two overlapping proactive-execution modules** — `pulse` (heartbeat) and `scheduler` (cron) duplicate timer, config, and policy code. Neither has circuit breakers, drift compensation, or multi-instance leader election.
5. **Self-modification surface is undefined** — agents cannot create skills, tools, or extensions programmatically despite this being a core differentiator. Pattern must exist without opening RCE or policy bypass.

## 2. Success Criteria

**Ship-blocking (required for spec completion):**

- **S1** — All 6 known bugs fixed, each with a regression test. *(Simplicity)*
- **S2** — Tool policy pipeline enforces on every tool call (execution-time, not registration-time) with 0 fail-open paths. *(Security)*
- **S3** — Read-only tool batches execute in parallel; any state-modifying tool forces sequential. Zero races in adversarial tests. *(Scalability + Security)*
- **S4** — `pulse` and `scheduler` replaced by unified `ProactiveEngine` with drift-free timer, circuit breaker per schedule, and leader-election-safe multi-instance behavior. *(Modularity + Scalability)*
- **S5** — Agents can create skills in all tiers, tools in enterprise/personal tiers, extensions in personal tier only — each action audit-logged, each artifact AST-validated + restricted-builtin-sandboxed + egress-proxied. *(Security + Modularity)*
- **S6** — Adversarial security suite (import bypass, capability composition, policy bypass) passes 100%. *(Security)*
- **S7** — `arc agent chat` REPL commands `/sandbox` and `/strategy` become functional. *(Simplicity)*
- **S8** — CLI mirror exists for every new programmatic capability (schedule management, tool creation, policy inspection). *(Modularity — scriptability/CI)*

**Quality gates (from CLAUDE.md):**

- Line coverage ≥ 80% overall, ≥ 90% on new core components
- `mypy --strict` clean
- `ruff check` clean
- No new critical/high vulnerabilities
- Audit trail emitted for every new operation

## 3. Out of Scope

- Rewriting `arcllm`, `arcteam`, `arcui`, `arctui` internals
- Replacing the message bus (NATS is fixed)
- Changing identity/DID model (SPEC-007 is the source of truth)
- New provider adapters in `arcllm`
- UI changes in `arcui` (events flow through existing bridge)

## 4. Users & Personas

| Persona | Needs |
|---------|-------|
| **Federal operator (DOE)** | Air-gapped deploy, deterministic behavior, full audit trail, self-modification DISABLED |
| **Enterprise operator** | Observable deployment, approval gates on consequential actions, scriptable via CLI |
| **Personal developer** | Fast iteration, self-modification ENABLED, sensible defaults |
| **Agent author** | Predictable tool dispatch semantics (what runs parallel, what runs sequential), structured completion signal |
| **Compliance auditor** | Reconstruct full system state from audit log alone |

## 5. Requirements (EARS Format)

Each requirement is prefixed with its ID and tagged with **[Pillar]**.

### 5.1 Integration Gaps (S1 — Known Bugs)

- **R-001** **[Simplicity]** WHEN `ArcAgent` is instantiated with an LLM model via `load_eval_model()`, THE system SHALL wire the bridge's `on_event` callback to the model so that LLM events flow into the ModuleBus.
- **R-002** **[Modularity]** THE `arcagent/modules/ui_reporter/` module SHALL ship with a `MODULE.yaml` so the convention-based `ModuleLoader` discovers and loads it.
- **R-003** **[Simplicity]** WHEN a user types `/sandbox` or `/strategy` in `arc agent chat`, THE REPL SHALL execute the command (not just display help text).
- **R-004** **[Security]** WHEN `ArcAgent.shutdown()` is called, THE system SHALL close the httpx client held by the LLM model so the process terminates without leaked connections.
- **R-005** **[Scalability]** WHEN the messaging module acks a message, THE system SHALL pass the actual `byte_pos` so JSONL seek optimization functions correctly.
- **R-006** **[Modularity]** ALL currently-hardcoded constants (`_CHECK_CIRCUIT_BREAKER_THRESHOLD`, `_MAX_STEERING_MESSAGE_LEN`, WebSocket URL, OTEL endpoint) SHALL move to package-local TOML config with sensible defaults.

### 5.2 Tool Policy Pipeline (S2)

- **R-010** **[Security]** WHEN a tool call is dispatched, THE Tool Policy Pipeline SHALL evaluate it through N layers in strict order: Global → Provider → Agent → Team → Sandbox. Federal uses all 5; Enterprise uses 4 (no Team); Personal uses Global only.
- **R-011** **[Security]** IF any layer returns DENY, THE pipeline SHALL short-circuit, record the denial with `(layer, rule_id, input_values)`, emit an audit event, and abort the tool call. **First-DENY-wins.**
- **R-012** **[Security]** IF a layer raises an exception, THE pipeline SHALL treat it as DENY (fail-closed) and emit an error-level audit event. NEVER propagate as ALLOW.
- **R-013** **[Scalability]** THE pipeline SHALL complete p95 evaluation in < 1ms using indexed rule lookup (by tool name) and a 30-60s LRU cache keyed on `(agent_did, tool_name, classification)`.
- **R-014** **[Security]** THE pipeline SHALL emit structured deny reasons answering three questions: (1) which layer, (2) which rule, (3) what input values triggered it. Example: `"Tool requires SECRET clearance; agent has FOUO"`, not `"Access denied"`.
- **R-015** **[Security]** Classification propagation: WHEN Agent A calls Tool T that returns classified data, AND A delegates to Agent B, THE classification context SHALL propagate to B's subsequent tool calls. No write-down to lower-classification targets.
- **R-016** **[Security]** Dynamic tool registration SHALL be a privileged action governed by the Global layer. Newly registered tools SHALL be classified before callable; scope is the creating session, not globally visible.
- **R-017** **[Security]** THE pipeline SHALL support dry-run / shadow mode: evaluate and log denials without blocking. Required for safe policy hot-reload.
- **R-018** **[Security]** Air-gapped posture: policy bundles SHALL be signed and locally cached; when the control plane is unreachable past `max_bundle_age`, THE pipeline SHALL enter restricted mode (deny all except hardcoded safe set).

### 5.3 Parallel Tool Execution (S3)

- **R-020** **[Simplicity + Modularity]** EVERY tool SHALL be classified at registration time as `read_only` or `state_modifying`. Classification is part of the tool contract.
- **R-021** **[Scalability]** WHEN a strategy dispatches a batch of tool calls AND all are `read_only`, THE runtime SHALL execute them concurrently via `asyncio.gather(return_exceptions=True)` bounded by `asyncio.Semaphore(max_parallel_tools)` (default 10, configurable).
- **R-022** **[Security]** WHEN a batch contains ANY `state_modifying` tool, THE runtime SHALL execute the entire batch sequentially. Implicit dependency (one tool's path argument appears as another's path result) also forces sequential.
- **R-023** **[Scalability]** Partial failure is acceptable: a failed `read_file` SHALL NOT abort a concurrent `web_search`. Per-tool errors are captured in the result list and logged as structured events.
- **R-024** **[Security]** Audit ordering SHALL use a monotonic sequence number assigned at dispatch (pre-gather). Each event records `{seq, tool_id, dispatch_ts, complete_ts, status}`.
- **R-025** **[Scalability]** FIPS / air-gapped mode SHALL cap `max_parallel_tools` at 4 for HTTPS-touching tools due to TLS handshake contention on constrained VMs.

### 5.4 Loop Termination & Limits (S3)

- **R-030** **[Simplicity]** THE runtime SHALL expose a built-in `task_complete` tool with schema:
  ```
  task_complete(
    status: "success" | "partial" | "failed",  # required
    summary: str,                                # required
    artifacts: list[str] | None,
    next_steps: list[str] | None,
    error: str | None,
  )
  ```
- **R-031** **[Modularity]** WHEN the loop receives a `task_complete` tool call, THE loop SHALL terminate cleanly, emit a `loop.completed` bus event, and return the result payload.
- **R-032** **[Security]** THE loop SHALL enforce `max_turns` and `max_cost_usd` (defaults 100 and $5.00, configurable). On breach, THE loop SHALL call `task_complete(status="failed", error="max_turns" | "max_cost")`. Federal: hard cap. Enterprise: auto-approve 2x. Personal: always approve.

### 5.5 ProactiveEngine (S4)

- **R-040** **[Modularity]** `arcagent/modules/pulse/` and `arcagent/modules/scheduler/` SHALL be replaced by a single `arcagent/modules/proactive/` module. The old modules SHALL be deleted (no shim, no compat layer) with a one-line migration note in the changelog.
- **R-041** **[Simplicity]** THE ProactiveEngine SHALL use a single asyncio task driving a min-heap priority queue sorted by `next_run`. Polling granularity: 1–5 seconds.
- **R-042** **[Security + Scalability]** Drift prevention: `next_run = last_actual_run + interval`, NEVER `next_run = now + interval`. Small negative scheduling-overhead adjustment (-0.010s) per cycle. Jitter parameter for herd prevention.
- **R-043** **[Scalability]** Clock source: `time.monotonic()` for interval math (maps to `CLOCK_BOOTTIME` on Linux 3.x+, includes suspend). Wall clock used only for active-hours checks. Warp detection: compare `time.time()` delta against `time.monotonic()` delta; warn if divergence > 5s.
- **R-044** **[Security]** Heartbeat isolation: heartbeat tool outputs SHALL NOT enter the agent's main context window. Heartbeat runs as a stateless side-channel call with minimal context. The decision boundary SHALL be `{idle, not_idle}` — never `{idle, act_on_X, act_on_Y}`.
- **R-045** **[Security + Scalability]** Circuit breaker per schedule (Resilience4j state machine): CLOSED → OPEN → HALF_OPEN → CLOSED. Exponential backoff: `min(60 * 2^open_count, 1800)` seconds. `auto_recovery_enabled = true`. Expose `force_close()` and `force_open()`.
- **R-046** **[Scalability]** Concurrency policy: IF the previous execution of a schedule is still running, THE engine SHALL skip the new tick and record `miss`. (Kubernetes CronJob `concurrencyPolicy: Forbid`.)
- **R-047** **[Security]** Wake events SHALL be idempotent: compare `event.timestamp` against `last_wake_handled`; discard stale wakes.
- **R-048** **[Scalability]** Multi-instance (10–20 agents): ProactiveEngine runs on the leader only via Kubernetes Lease OR Redis lock. All scheduled actions SHALL be idempotent (at-least-once semantics).
- **R-049** **[Modularity]** Timezone handling: store UTC internally, accept IANA timezone for user-facing config, convert at schedule creation only. Overnight windows (22:00–06:00) detected as `end < start`. DST: skip spring-forward, no double-execute on fall-back.

### 5.6 Self-Modification Surface (S5)

- **R-050** **[Modularity]** THE agent SHALL expose exactly 6 self-modification tools:
  1. `create_skill(name, markdown_body)` — all tiers
  2. `improve_skill(name, new_markdown_body)` — all tiers
  3. `create_tool(name, python_source)` — federal DENIED, enterprise approval, personal allowed
  4. `create_extension(name, python_source, module_yaml)` — federal DENIED, enterprise approval, personal allowed
  5. `list_artifacts(kind)` — all tiers
  6. `reload_artifacts()` — all tiers (read-only refresh)
- **R-051** **[Simplicity]** Agent-created tools SHALL be single `.py` files with a `@tool` decorator (no multi-file packages). Schema SHALL be derived from type hints via Pydantic `validate_call` — AI never writes schema manually.
- **R-052** **[Security]** Dynamic tool loading SHALL use `importlib` with unique module names `f"_agent_tools.{name}_{hash(path)}"`. Tools SHALL NOT be registered in `sys.modules`.
- **R-053** **[Security]** AST pre-validation SHALL block: `ctypes`, `sys.modules` access, `.gi_frame / .f_back / .f_builtins / .f_globals` attribute reads, `compile() + eval()` combinations, `pickle` / `__reduce__`, `__class__.__base__.__subclasses__()`, dynamic `getattr(__builtins__, ...)`, `string.Formatter`, non-UTF-8 source encoding declarations.
- **R-054** **[Security]** Restricted-builtin dict: dynamic tools execute with a scrubbed `__builtins__` dict — `print`, `len`, `range`, `str`, `int`, `float`, `bool`, `list`, `dict`, `set`, `tuple`, `sorted`, `min`, `max`, `sum`, `enumerate`, `zip`, `map`, `filter`, `any`, `all`, `isinstance`. Explicit denylist takes precedence on conflict.
- **R-055** **[Security]** Network egress: dynamic tools SHALL ONLY reach the network via `ToolContext.http`, a proxy that enforces a per-tool allowlist of endpoints. Deny-by-default. Every request is logged with `(tool, endpoint, method, status)`.
- **R-056** **[Modularity]** Hot-reload SHALL be immediate on create (no watcher process). `reload_artifacts()` rescans disk and updates the registry.
- **R-057** **[Simplicity]** Name collision default: `"warn"` — replace with warning logged (FastMCP pattern). Configurable via `tool_registry.on_collision = "error" | "replace" | "warn" | "ignore"`.
- **R-058** **[Security]** Every self-modification action SHALL emit an audit event with `(actor_did, action, artifact_name, content_hash, tier, outcome)`.

### 5.7 Observability (S6 Supporting)

- **R-060** **[Security]** Every policy evaluation SHALL emit an OTel span + bus event with: `request_id`, `session_id`, `layer`, `policy_version`, `decision`, `matched_rule`, `evaluation_time_us`, `input_hash`.
- **R-061** **[Security]** THE system SHALL expose these metrics: denial rate by layer, evaluation latency p50/p95/p99, exception rate (non-zero = bug), cache hit rate, circuit breaker state per schedule, heartbeat tick rate, heartbeat silent-suppression count.

### 5.8 Testing (S6)

- **R-070** **[Security]** Adversarial security suite SHALL include tests for every bypass class listed in Decision 21: import bypass (8 scenarios), capability composition (3 scenarios), policy pipeline (5 scenarios). 100% pass required.
- **R-071** **[Security]** Adversarial suite SHALL run in CI as a gating check. Adding a new tool or policy layer requires extending the suite.
- **R-072** **[Simplicity]** Test coverage ≥ 90% on: `core/tool_policy.py`, `modules/proactive/engine.py`, `tools/_dynamic_loader.py`, `tools/completion.py`. ≥ 80% overall.

### 5.9 CLI (S8)

- **R-080** **[Modularity]** Every new programmatic capability SHALL have a CLI equivalent for scriptability and CI/CD. Minimum surface:
  - `arc agent schedule {create|list|pause|resume|delete}`
  - `arc agent tool {create|list|reload|inspect}`
  - `arc agent skill {create|improve|list}`
  - `arc agent policy {inspect|shadow|validate}`
  - `arc agent completion history` (recent `task_complete` events)

## 6. Federal / Enterprise / Personal Tier Matrix

| Requirement | Federal | Enterprise | Personal |
|-------------|---------|------------|----------|
| R-010 layers | 5 (G/P/A/T/S) | 4 (no Team) | 1 (Global) |
| R-032 max_turns | hard cap | auto-approve 2x | always approve |
| R-050 `create_tool` | DENIED | approval | allowed |
| R-050 `create_extension` | DENIED | approval | allowed |
| R-055 egress | deny-by-default + signed allowlist | deny-by-default | deny-by-default + warning only |
| R-048 leader election | required | required | optional |

Tier is set per-package via TOML, not globally (Decision 20).

## 7. Auto-Applied Federal Mandates (NIST 800-53)

These are not negotiable requirements; they are compliance mandates applied automatically.

| ID | Control | Applied To |
|----|---------|-----------|
| M-1 | AU-2 | Every policy evaluation audit-logged |
| M-2 | AU-2 | `task_complete` events audit-logged |
| M-3 | OMB A-123 / FITARA | Budget tracking mandatory (federal) |
| M-4 | AU-2 | Self-mod actions audit-logged (`create_skill`, `create_tool`, `create_extension`) |
| M-5 | AU-2 | Proactive execution audit-logged with trigger type |
| M-6 | AU-11 | Audit retention minimum 1 year |
| M-7 | AU-9 | Agent-created code cannot bypass audit |
| M-8 | AU-2 | Schedule changes audit-logged |
| M-9 | SI-7(15), CM-5, CM-8 | Dynamic code DENIED in federal tier |
| M-10 | SC-7 | Network egress deny-by-default |

## 8. Known Constraints

- **Core LOC budget (CLAUDE.md, ADR-004):** Core currently exceeds the 3,500 ceiling. `tool_policy.py` is new core surface — must either enforce an ADR-004 increase or extract non-core code first. Decision: ADR-004 raises ceiling to 5,000 (see SDD §Module Budget).
- **RestrictedPython CVE history** (CVE-2023-37271, 2025-22153, 2024-47532): AST scanning alone is insufficient. Layered defense is mandatory (R-053 through R-055).
- **Non-compositional safety** (arXiv:2603.15973): Two individually-safe tools can compose into forbidden capabilities (read + http = exfil). Capability inventory required at deployment (covered in SDD §Threat Model).

## 9. Non-Functional Requirements

| Category | Target | Pillar |
|----------|--------|--------|
| Policy eval p95 | < 1ms | Scalability |
| Cold start overhead | < 50ms added by pipeline | Scalability |
| Memory per policy cache | < 10MB at 10k entries | Scalability |
| ProactiveEngine tick jitter | < 50ms p95 | Scalability |
| Audit write latency | < 5ms p95 (buffered) | Security |
| Core LOC | < 5,000 (post-ADR-004) | Simplicity |
| `mypy --strict` | clean | Simplicity |
| `ruff check` | clean | Simplicity |

## 10. Acceptance Criteria Traceability

Every `S#` success criterion maps to a set of `R-###` requirements:

| Success | Requirements |
|---------|--------------|
| S1 | R-001 … R-006 |
| S2 | R-010 … R-018, R-060, R-061 |
| S3 | R-020 … R-025, R-030 … R-032 |
| S4 | R-040 … R-049 |
| S5 | R-050 … R-058 |
| S6 | R-070 … R-072 |
| S7 | R-003 |
| S8 | R-080 |

Each requirement is implementation-tested in PLAN.md tasks and verified in PRD acceptance gates (README §Acceptance Gates).
