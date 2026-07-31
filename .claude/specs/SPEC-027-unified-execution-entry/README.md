---
spec_id: SPEC-027
name: unified-execution-entry
status: complete
created: 2026-05-31
type: integration
intake_confidence: 0.94
type_confidence: 0.70
fast_track: true
prior_work:
  - docs/architecture/decisions/ADR-024-unified-streaming-run-entry.md (authoritative: one streaming session-bound run)
  - docs/architecture/decisions/ADR-023-capability-resolution-and-arcrun-provider.md (CapabilityProvider contract)
  - docs/architecture/decisions/ADR-020-arcgateway-as-data-plane.md (session/channel model)
  - .claude/brainstorms/2026-04-28-unified-capability-system.md (capability model + OpenClaw comparison)
  - .claude/decisions-log.md D-122 (scheduler = agent.run + fresh session), D-163 (modules bind agent_run_fn/agent_chat_fn)
related_specs:
  - SPEC-026-arcstore-operational-storage (Observe/audit must keep working unchanged through the new path)
pillars_priority: [Simplicity, Modularity, Security, Scalability]
steering_status: present (.claude/steering/) — conforms to tech.md package layout, structure.md module boundaries
---

# SPEC-027 — Unified Execution Entry

## TL;DR

Collapse the agent execution surface to **one** entry: `agent.run(input, *, session) -> AsyncIterator[StreamEvent]`. Always session-bound, always streaming. Every surface — chat (UI / Slack / Telegram), CLI, scheduler, MAS — drives the agent the same way, through arcrun. The flat tool list becomes the unified **`CapabilityProvider`** (ADR-023). The arcgateway executor consumes the real arcrun stream and emits real token deltas, finishing the long-standing M2 "fake streaming" TODO.

Routing is already unified (everything reaches `agent.* → arcrun`). This spec removes the **interface** fork: four overlapping entry methods (`run`/`run_async`/`run_stream`/`chat`), a streaming path that exists but isn't wired, and a chat path that pretends to stream.

## Decisions Log (from ADR-024 / ADR-023)

| ID | Decision | Rationale | Source |
|---|---|---|---|
| D-001 | One entry `agent.run(input, *, session)`; delete `chat`/`run_async`/`run_stream` | One way to run an agent from any surface; shrinks the arcagent nucleus | ADR-024 |
| D-002 | `run` is **always session-bound** (mandatory `Session`) | Uniform history/audit/channel behavior; no "has-session vs not" divergence | ADR-024 |
| D-003 | `run` **always streams** arcrun `StreamEvent`s; one-shot callers `collect()` | Single code path; chat streams for real | ADR-024 |
| D-004 | arcgateway executor consumes the stream → real token Deltas; delete the wrap-as-one-token branch | Finishes the M2 TODO; kills the chat/run fork in the executor | ADR-024 |
| D-005 | `arcrun.run` takes a **`CapabilityProvider`** (advertise/load/invoke), not a flat `to_arcrun_tools()` list | Lazy, model-driven context; one contract not three | ADR-023 |
| D-006 | Module callbacks (`agent_run_fn`) and the scheduler bind the **same** unified `run` | No second entry for modules/scheduler (supersedes D-163's `set_agent_chat_fn`) | ADR-024, D-163 |
| D-007 | No back-compat shims — delete old methods in the same change | Local-only repo; CLAUDE.md no-legacy rule | CLAUDE.md |
| D-008 | Federal tier gates whether workspace-authored capabilities are callable | Secure-by-default; agent-authored Python denied at AST-load in federal | ADR-023; brainstorm |

## References

- **ADR-024** — one streaming session-bound run (authoritative)
- **ADR-023** — CapabilityProvider + layered roots + last-wins + signed-to-load
- `packages/arcagent/src/arcagent/core/agent.py` — `run`/`run_async`/`chat` (collapse target)
- `packages/arcagent/src/arcagent/core/agent_dispatch.py` — `arcrun_run`/`run_async`/`run_stream` + `to_arcrun_tools()`
- `packages/arcgateway/src/arcgateway/executor.py` — M2 fake-stream branch (lines ~233-260)
- `packages/arcrun/src/arcrun/loop.py` — `run(...)` signature (takes `tools: list[Tool]` today)
- `packages/arcagent/src/arcagent/modules/pulse/__init__.py`, scheduler — `agent_run_fn` callback binders

## Status Log

| Date | Status | Note |
|---|---|---|
| 2026-05-31 | PENDING | Spec generated from ADR-023/024 + unified-capability brainstorm. Fast-track (intake 0.94). Awaiting approval to implement. |
| 2026-05-31 | COMPLETE ✅ | `/implement` — **Phase D: every surface routed through the one entry + SPEC-026 verified (FR-2/FR-5).** CLI `arc agent run`/`chat`, the subprocess `agent_worker`, and arctui migrated to the streaming `agent.run(input, session=agent.session(key))` (+ `collect` for one-shot callers); the arctui chat_stream/blocking fork collapsed to one streaming path. Modules/scheduler were already on the one `run_collected` callback (Phase A). Arch test `test_single_entry.py` guards that no `chat`/`run_async`/`chat_async`/`chat_stream`/`set_agent_chat_fn`/`chat_fn` remain and the agent exposes only `run`/`run_collected`/`session`. `test_run_records_to_arcstore.py` proves SPEC-026 run_event recording fires through the new path tagged with the agent DID (the old dispatch never passed `actor_did` — now completed). Fixed two pre-existing CLI bugs (missing `context` arg; `/session` referenced removed singular `_session`). **Whole-spec gate: arcrun 416 · arcagent 3251 · arcgateway 740 · arccli 318 · arctui 67 · arcstore 56 · arctrust 182 — 0 failed; ruff + mypy --strict clean on changed src; arcagent core LOC 4392 (down from 4683 — NFR-1 met).** Status → COMPLETE. Deviations (see PLAN): `to_arcrun_tools` kept as internal helper; CLI output simplified to RunResult fields; agent-level arcstore `enabled` gate is a SPEC-026 follow-up. |
| 2026-05-31 | PHASE C ✅ | `/implement` — **arcrun takes a `CapabilityProvider` (FR-4 / ADR-023).** New `arcrun.capabilities`: `CapabilityProvider` Protocol (advertise/load/invoke), `CapabilitySpec`/`CapabilityResult`, `StaticProvider`, and `provider_tools` (builds the loop's internal tools from `advertise()`, routing dispatch through `invoke` with `caller_did`; skills fold into a built-in `use_skill` meta-tool that calls `load` — lazy, model-driven). `run`/`run_async`/`run_stream` now take `capabilities` instead of `tools: list[Tool]`; arcrun's event/cancellation/timeout machinery is unchanged (wraps invoke). Context-dependent tools (spawn) dispatch via a `raw_tools()` escape hatch that preserves the live `ToolContext`. arcagent: `AgentCapabilityProvider` over the capability registry — lean advertise (no skill bodies), `load` reads `SKILL.md` on demand, `invoke` routes through the core ToolRegistry's policy-wrapped execute (fail-closed), federal tier gates workspace-authored capabilities. Migrated 12 arcrun test files + spawn child-loops + spawn integration fakes to `StaticProvider`. **arcrun 416 passed; arcagent 3247 passed, 16 skipped, 0 failed; arch test (arcrun imports no arc sibling) green; ruff + mypy --strict clean on changed src.** Deviations (see PLAN): `to_arcrun_tools` kept as internal policy-wrapping helper (AC-4.3 intent met — arcrun seam takes the Protocol); `raw_tools()` for ctx tools; caller_did = agent DID. Phase D (CLI/TUI/scheduler-binding + arch tests + SPEC-026 end-to-end) remains. |
| 2026-05-31 | PHASE B ✅ | `/implement` — **Real streaming through the gateway (FR-3) — M2 TODO finished.** `AsyncioExecutor._stream` now `await agent.session(event.session_key)` then `async for ev in agent.run(message, session=...)`, adapting each `arcrun.TokenEvent` → token `Delta` (`is_final` only on the done sentinel). Deleted the `hasattr(agent,"chat")` fork, the wrap-whole-reply-as-one-token path, and the M2 comment. Error mid-stream fails closed (fail-closed Delta + done, nothing escapes the socket — AC-3.2); streaming is pull-based so a slow consumer never runs the producer ahead (AC-3.3; per-socket queue bound stays in the `in_process`/`web` adapters). Migrated the two end-to-end fakes (`personal_tier`, `dual_adapter`) to `session()`+streaming `run()`. **arcgateway 740 passed, 1 skipped, 0 failed; ruff + mypy --strict clean.** |
| 2026-05-31 | PHASE A ✅ | `/implement` — **One streaming, session-bound `agent.run` (FR-1).** arcrun: added `RunResult` + `collect()` and extended `run_stream` to forward `messages`/`on_event`(recording bridge)/`transform_context`/`tool_choice` (parity with the blocking path) — **410 passed**, ruff+mypy clean. arcagent: collapsed `run`/`run_async`/`chat`/`chat_async`/`chat_stream` into ONE `async def run(self, input, *, session: SessionManager) -> AsyncIterator[StreamEvent]`; `agent_dispatch` 3-way fork → one `dispatch_stream`; deleted `AgentHandle`/steering (test-only callers). **User-directed design: agent now holds a keyed pool of sessions (`dict[str, SessionManager]`) — different humans/agents are distinct concurrent sessions** (replaces the single re-pointed `self._session`, the latent miss-bug source); `agent.session(key)` = open-or-resume; `SessionManager.open_or_resume` added. `agent:ready` now emits one `run_fn = run_collected(input, *, session_key) -> RunResult`. Migrated all 5 in-arcagent surfaces (pulse/scheduler/slack/telegram/messaging) + `__main__` serve loop + ~5 test files to the unified callback. **arcagent 3241 passed, 16 skipped, 0 failed; ruff clean; mypy --strict clean on all changed src.** Deviations (see PLAN): SessionManager-is-Session (no wrapper, per user); minimal entry drops `tool_choice`/`automated`/cron `disabled_toolsets` (the old `CRON_AGENT_KWARGS` self-scheduling guard was stub-only — **re-home to arctrust policy in Phase C**). Phases B (gateway streaming), C (CapabilityProvider), D (CLI/TUI/arch tests) remain. |
