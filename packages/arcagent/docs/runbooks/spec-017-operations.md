# SPEC-017 Operations Runbook

Operational guide for the arc-core-hardening surface introduced in
SPEC-017: policy pipeline, proactive engine, dynamic tool surface,
and tier-aware self-modification.

## Table of contents

- [Policy pipeline](#policy-pipeline)
- [Proactive scheduling](#proactive-scheduling)
- [Tier configuration](#tier-configuration)
- [Metrics and observability](#metrics-and-observability)
- [Incident response](#incident-response)

---

## Policy pipeline

### What it does

Every tool call dispatched via `ToolRegistry` (when constructed with
`policy_pipeline=...`) flows through a 5-layer evaluator:

```
GlobalLayer → ProviderLayer → AgentLayer → TeamLayer → SandboxLayer
```

First DENY wins; exceptions fail-closed (treated as DENY). Allowed
calls propagate to the tool's `execute()`. Denied calls raise
`arcagent.core.tool_policy.PolicyDenied` with the full
`Decision` payload attached.

### Reading a deny decision

`PolicyDenied` answers three questions in its formatted message:

```
[layer:rule_id] reason-text
```

Example:

```
[agent:agent.allowlist] Tool 'bash' not in agent allowlist for
did:arc:research/alpha; agent has ['grep', 'read']
```

The same information is emitted as a `policy.evaluate` audit event
through the pipeline's `audit_sink` callback. Operators wire that
callback to their own metrics/telemetry backend to derive counters
(the pipeline ships no bundled metric registry).

### Tier construction

```python
from arcagent.core.tool_policy import build_pipeline

pipeline = build_pipeline(
    tier="federal",   # federal | enterprise | personal
    global_deny_rules={"bash": "global.denylist: subprocess requires review"},
    agent_allowlists={"did:arc:alpha": {"read", "grep"}},
    forbidden_compositions=[frozenset({"file_read", "network_egress"})],
    cache_ttl_seconds=30.0,
    max_bundle_age_seconds=600.0,
    safe_set={"read", "grep"},   # restricted-mode allowed tools
    shadow=False,                # True = log but allow (staged rollout)
    audit_sink=audit_callback,
)
```

### Shadow-mode rollout

New policy bundles should be staged with `shadow=True` for at least
one evaluation cycle. In shadow mode the pipeline evaluates every
call, emits the would-be decision to the audit trail, and always
returns ALLOW. Operators watch the `policy.evaluate` audit stream for
`deny` decisions under shadow; if the count is reasonable the bundle
is promoted with `shadow=False`.

---

## Proactive scheduling

### What it does

`ProactiveEngine` drives time-based schedules (cron-like intervals
and agent heartbeats) from a single asyncio task backed by a min-heap
priority queue. Replacing the legacy `pulse` and `scheduler` modules.

### Adding a schedule

```python
from arcagent.modules.proactive import (
    CircuitBreaker, ProactiveEngine, Schedule,
)

engine = ProactiveEngine(handler=dispatch_to_agent)
engine.add(
    Schedule(
        id="hourly-ingest",
        interval_seconds=3600,
        next_run_monotonic=time.monotonic() + 5,
        kind="cron",
        circuit_breaker=CircuitBreaker(
            failure_threshold=3,
            base_wait_seconds=60,
            max_wait_seconds=1800,
        ),
        jitter_seconds=30.0,
    )
)
```

### Drift-free semantics

Reschedule uses `last_actual_run + interval - 0.010`. If a tick is
**skipped** (circuit open or prior run still in-flight), reschedule
uses `now + interval - 0.010` instead — prevents heap-spin replay of
the same due timestamp.

### Clock warp detection

Call `engine.check_clock_warp(monotonic_delta=..., wall_delta=...)`
once per tick with the observed deltas between successive ticks.
Divergence ≥ `clock_warp_threshold_seconds` (default 5s) emits a
`clock_warp` event. Does not halt execution — the engine keeps
ticking so operators can correlate.

### Leader election

Multi-instance deployments MUST use a real leader election backend
(Kubernetes Lease or Redis lock). Single-instance / personal tier
uses `NoOpLeaderElection`. The `LeaderElection` Protocol
(`acquire_or_wait`, `release`, `is_leader`) is the only contract —
write a thin adapter against your infrastructure of choice.

All scheduled actions MUST be idempotent. The engine provides
at-least-once semantics across failover.

---

## Tier configuration

| Capability | `federal` | `enterprise` | `personal` |
|------------|-----------|--------------|------------|
| `create_skill` / `improve_skill` | ✓ | ✓ | ✓ |
| `create_tool` | ✗ DENIED | ✓ (with audit) | ✓ |
| `create_extension` | ✗ DENIED | approval required | ✓ |
| Policy layers | 5 (G/P/A/T/S) | 4 (no Team) | 1 (Global only) |
| Dynamic egress allowlist | signed bundle | deny-by-default | deny + warn |
| Turn/cost limits | hard cap | auto-approve 2× | always approve |

Federal denial for `create_tool` is enforced BEFORE any dynamic
source reaches the capability loader's AST validator +
restricted-builtins compile stage — no code path can reach it. The
denial emits `self_mod.tool_create_denied` with `tier="federal"`.

---

## Metrics and observability

Observability is sink-based. The policy pipeline and proactive engine
emit structured `(event, payload)` tuples through operator-supplied
callbacks — there is no bundled metric registry or `/metrics`
renderer. Wire the callbacks to whatever telemetry backend you run
(OpenTelemetry, Prometheus client, log pipeline) and derive counters
from the event stream.

### Key event streams

| Source | Sink | Events | Purpose |
|--------|------|--------|---------|
| `PolicyPipeline` | `audit_sink` | `policy.evaluate` | Every decision (carries `layer`, `outcome`, `rule_id`, `reason`, timing). Spike in `deny` = potential regression; any exception surfaces as a fail-closed `deny`. |
| `ProactiveEngine` | `event_sink` | `clock_warp`, `missed_concurrency`, `skipped_inactive_hours`, `handler_error` | Schedule health — skipped ticks, warps, handler failures. |
| Self-mod dispatch | audit trail | `self_mod.tool_create_denied`, `dynamic_tool.rejected` | Dynamic tool creation attempts (carry `tier` / AST category). |

### Wiring the sinks

```python
def policy_sink(event: str, payload: dict) -> None:
    ...  # forward to your metrics/telemetry backend

def proactive_sink(event: str, payload: dict) -> None:
    ...  # forward to your metrics/telemetry backend

pipeline = build_pipeline(
    tier="federal",
    audit_sink=policy_sink,
)
engine = ProactiveEngine(
    handler=handler,
    event_sink=proactive_sink,
)
```

---

## Incident response

### Policy pipeline reporting many denies

1. Filter the `policy.evaluate` audit stream for `deny` decisions by
   layer — narrow down which layer is denying.
2. Inspect the associated audit events (`policy.evaluate` with
   `decision="deny"`). Each carries `layer`, `rule_id`, and
   `reason`.
3. If a new bundle was just promoted, consider re-running it under
   `shadow=True` to verify the deny count matches expectation.

### Clock warp detected

1. Check the `clock_warp` events from the proactive `event_sink`
   around the warp timestamp.
2. Confirm with platform team — VM suspend, NTP adjust, container
   migration all produce warps.
3. Engine keeps running; no action required unless warps are
   frequent (indicates clock instability).

### Circuit breaker stuck OPEN

1. Inspect the breaker state via `breaker.state` — confirms whether
   it is OPEN.
2. Review `handler_error` events to see why the breaker opened.
3. Use `breaker.force_close()` for manual recovery if the underlying
   issue is confirmed resolved. `force_open()` exists for intentional
   disabling.

### Dynamic tool load rejected unexpectedly

1. Inspect `dynamic_tool.rejected` audit events — each carries the
   AST category (`import:os`, `attribute:gi_frame`, etc.).
2. If the rejection is correct: the tool author must rewrite.
3. If the rejection is a validator bug: file an issue with the
   source + category. **Never** whitelist past the validator.

---

## Legacy module migration

`modules/pulse/` and `modules/scheduler/` remain on disk in this
release. Before deletion:

1. Drain in-flight schedules from both modules.
2. Export persisted schedule state (the modules use file-backed
   stores under `~/.arcagent/scheduler/`).
3. Import into the new `ProactiveEngine` via `engine.add(...)`.
4. Disable the legacy modules in `arcagent.toml` by setting
   `[modules.pulse].enabled = false` and
   `[modules.scheduler].enabled = false`.
5. Confirm the proactive `event_sink` continues emitting schedule
   events from the new engine.
6. Delete the legacy module directories in a dedicated commit.

This is documented as a **deferred migration** — not blocking for
the SPEC-017 release; the new engine runs alongside the legacy
modules without conflict.
