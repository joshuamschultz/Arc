# SPEC-027 — Unified Execution Entry: PRD

**Status:** PENDING · **Type:** integration (cross-module: arcagent ↔ arcrun ↔ arcgateway)
**Pillar priority:** Simplicity → Modularity → Security → Scalability

## Problem

The agent has **four** ways in (`agent.run`, `run_async`, `run_stream`, `chat`), `agent_dispatch` builds the tool list three times, the gateway executor wraps a whole reply as one fake "token" (the M2 TODO) while a real streaming path sits unused, and arcrun receives a flat `list[Tool]` that forces skills to either burn context every turn or be dropped. Modules and the scheduler bind yet another callback shape (`agent_run_fn`). The result: one logical operation ("run a turn") implemented four ways, with a fake-streaming chat path and no lazy capability loading.

## Goal

One entry, one contract, one stream — used by every surface.

## Non-Goals

- Re-architecting sessions or the arcgateway channel model (ADR-020 stands).
- Building Postgres/cloud backends, or changing SPEC-026 Observe/audit (spool/WORM recording is unchanged — it must keep working through the new path).
- Implementing every capability source's signing pipeline (arcskill hub already exists); this spec *consumes* the verified registry, it does not rebuild it.

## Requirements (EARS) — each acceptance criterion tags its governing pillar

### FR-1 — One streaming, session-bound `agent.run` (P0)
The agent SHALL expose exactly one execution entry: `async def run(self, input, *, session: Session) -> AsyncIterator[StreamEvent]`.

- **AC-1.1** — `agent.chat`, `agent.run_async`, and `agent.run_stream` no longer exist (grep test: zero references in non-test source). **Simplicity.**
- **AC-1.2** — `run` requires a `Session`; calling it without one is a type error / raises. **Modularity.**
- **AC-1.3** — `run` yields arcrun `StreamEvent`s (token … turn-end); a provided `collect(stream) -> RunResult` helper returns the final result for one-shot callers. **Simplicity.**
- **AC-1.4** — A turn driven through `run` appends to the session's `SessionManager` history exactly as `chat` did (history parity test). **Modularity.**

### FR-2 — Every surface goes through the one entry (P0)
Chat (gateway), CLI, scheduler, and module callbacks SHALL all drive the agent via `run`.

- **AC-2.1** — The arcgateway executor calls `run` and streams real per-token `Delta`s; the "wrap whole reply as one token" branch and the `chat`-vs-`run` fork are deleted. **Simplicity.**
- **AC-2.2** — `arc agent run` (CLI) opens-or-resumes a deterministic local session and `collect()`s the stream to a final result. **Simplicity.**
- **AC-2.3** — Modules/scheduler bind the unified `run` (one `agent_run_fn` shape); `set_agent_chat_fn` is gone. A pulse/scheduler fire runs a full turn through `run` (D-122). **Modularity.**
- **AC-2.4** — No code path reaches arcrun except through `agent.run` (except the documented out-of-agent `arc llm` direct caller). Import/grep test. **Modularity.**

### FR-3 — Real streaming end to end (P0)
The stream SHALL surface arcrun token events to the channel as they arrive.

- **AC-3.1** — A multi-token reply yields ≥2 `Delta`s with `is_final` only on the last (no single-chunk wrapping). **Simplicity.**
- **AC-3.2** — An error mid-stream terminates the iterator cleanly (the turn fails closed; the session records the failure; the socket closes without a partial-success claim). **Security.**
- **AC-3.3** — Backpressure: a slow consumer does not unbounded-buffer the producer (bounded per-socket queue, existing). **Scalability.**

### FR-4 — arcrun takes a `CapabilityProvider`, not a flat tool list (P0)
`arcrun.run` SHALL accept a `CapabilityProvider` (ADR-023: `advertise`/`load`/`invoke`); arcagent implements it over the existing `CapabilityLoader`/registry.

- **AC-4.1** — `advertise()` puts only lean specs (name · kind · "use when" · input_schema) into the model context; no skill bodies. **Simplicity / Scalability.**
- **AC-4.2** — A skill body enters context only after the model selects it (`use_skill(name)` → `load(name)` → spliced into the next turn). Unused skills cost ~1 line. **Scalability.**
- **AC-4.3** — `to_arcrun_tools()` (flat list) is removed; arcrun no longer imports a concrete tool list type — only the `CapabilityProvider` Protocol. **Modularity.**
- **AC-4.4** — Every `invoke`/`load` carries `caller_did` and passes through `arctrust` policy before running; a denied capability fails closed. **Security.**

### FR-5 — Concern boundaries preserved (P0)
The change SHALL respect "don't mix concerns": arcrun owns the loop + the Protocol; arcagent owns assembly + capabilities; arcllm owns provider calls.

- **AC-5.1** — `arcrun` imports neither arcagent nor a concrete capability/skill type (import-graph test). **Modularity.**
- **AC-5.2** — SPEC-026 Observe/audit is unchanged: a turn through the new `run` still records `llm_call`/`run_events` to spool and `emit()`s to WORM (end-to-end test reads them back via arcstore). **Security.**

### FR-6 — Federal-tier capability gating (P1)
Whether agent-authored (`workspace/.capabilities`) capabilities are callable SHALL be tier-gated.

- **AC-6.1** — In federal tier, workspace-authored Python capabilities are denied at load (not advertised, not invocable); personal/enterprise allow them with audit. **Security.**

## Non-Functional

- **NFR-1 (Simplicity)** — Net arcagent core LOC must not increase (four entries → one should *reduce* it); stays under the 3,500 budget.
- **NFR-2 (Scalability)** — Streaming adds no per-turn allocation proportional to history; `advertise()` is O(capabilities), `load()` is O(1) per selected skill.
- **NFR-3 (Security)** — No capability is callable that wasn't signed-to-load and policy-allowed; every override audited (ADR-023).

## Acceptance (whole spec)

All FR acceptance criteria pass with fresh output; `ruff` + `mypy --strict` clean; SPEC-026 suites stay green; no `chat`/`run_async`/`run_stream`/`to_arcrun_tools`/`set_agent_chat_fn` references remain.
