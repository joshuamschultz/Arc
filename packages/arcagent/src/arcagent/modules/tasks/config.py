"""Configuration for the tasks module.

Owned by the tasks module — not part of core config.
Loaded from ``[modules.tasks.config]`` in arcagent.toml.
"""

from __future__ import annotations

from arcagent.modules.base_config import ModuleConfig


class TasksConfig(ModuleConfig):
    """Tasks module configuration.

    Inherits ``extra="forbid"`` from ModuleConfig for typo detection.
    """

    # Config-level enable mirrors the module-config convention (messaging et al.);
    # the load gate is ModuleEntry.enabled in the [modules.tasks] table.
    enabled: bool = False
    # Autonomous execution toggle (SPEC-056 Phase D). Off by default —
    # auto-running assigned work is agency the operator must opt into
    # explicitly (ASI01/LLM06), never on by mere module presence. When true,
    # the dispatch loop starts the agent's ready, owned tasks and runs them.
    dispatch: bool = False
    # Forwarded to ``arcstore.config.resolve_data_dir`` — empty string defers
    # to that function's own env > default precedence (SPEC-026 §13.2) so
    # this module and arcui always agree on which SQLite file is the durable
    # Task directory.
    data_dir: str = ""
    # NATS JetStream url for the shared arcteam registry (mirrors
    # MessagingConfig.nats_url). Empty means no live registry is built —
    # assign_task/create_task's @handle resolution degrades with a clear
    # error instead of silently building a useless, disconnected registry.
    nats_url: str = ""

    # --- Lifecycle reliability engine (SPEC-056 Phase 1) --------------------
    # Retry ceiling stamped onto tasks this agent creates; the per-task
    # ``max_attempts`` field is authoritative once set. A run that fails/errors/
    # times out is retried until this many attempts, then dead-lettered
    # (terminal ``failed``). 1 disables retry (single attempt).
    default_max_attempts: int = 3
    # Base backoff (seconds) before a retried task is re-dispatched; grows
    # exponentially per attempt (base * 2**(attempts-1)) so a flapping task
    # doesn't hot-loop (ASI08 cascade containment).
    retry_backoff_seconds: float = 30.0
    # Wall-clock cap (seconds) on a single dispatched run; 0 = unbounded. The
    # per-task ``timeout_seconds`` field overrides this. A timeout is treated as
    # a failed attempt (LLM10 unbounded-consumption guard).
    task_timeout_seconds: float = 0.0
    # An ``in_progress`` task with no live run (e.g. the agent crashed or was
    # restarted mid-run) older than this is reclaimed as a failed attempt so it
    # re-dispatches instead of being orphaned forever. Startup reclaim ignores
    # this threshold (any orphan from before the restart is reclaimed at once).
    stuck_reclaim_seconds: float = 300.0

    # --- Routing + notifications (SPEC-056 Phase 3/4) ----------------------
    # Auto-route ownerless tasks to the least-loaded eligible agent (capability
    # match preferred). No-op without a live registry, so safe on by default.
    # The heuristic is deliberately simple; goal-relevance routing is a seam.
    routing: bool = True
    # Notify the operator on key transitions (done/failed/needs-review/
    # escalation) and the assignee on assignment. Best-effort — a delivery
    # failure never blocks a state transition (AU still records it).
    notify: bool = True
