# Product Requirements Document: arctui — Terminal Agent Interface (v1)

## Context References

- **Brainstorm:** [.claude/brainstorms/2026-07-24-arctui-terminal-agent.md](../../brainstorms/2026-07-24-arctui-terminal-agent.md)
- **Build decisions:** `.claude/decisions-log.md` → "arctui — Terminal Agent Interface" (D-482…D-488 + Research Insights, deepened 2026-07-25)
- **Personas / metrics:** [.claude/steering/product.md](../../steering/product.md)
- **Tech / compliance:** [.claude/steering/tech.md](../../steering/tech.md)

## Product Overview

### Vision
arctui is a terminal *interface* to what arc already is — a way to run and control an arc agent from the command line, model-agnostic and local-first, whose wedge is an extensible ecosystem that is also secure by construction. It is not a new agent or a new extension system; it surfaces arc's existing capabilities (agents, tools/skills/modules, memory, MCPs, arcrun loops, arcllm models, hooks) through a terminal UI.

### Problem Statement
arc's power is only reachable through arcui (web) or the CLI's one-shot commands. There is no terminal agent you can drop into a project folder and code with — the way developers use Claude Code / pi — that is model-agnostic (open-source or frontier via arcllm) and carries arc's trust guarantees. A working arctui exists (~1,700 LOC Textual chat/monitor) but is not yet daily-usable for real coding: approvals are built but unwired, there is no folder-trust gate, no agent/model selection, and the loop's reliability signals are not surfaced.

### Value Proposition
A local terminal agent that is at least as usable as Claude Code for working on real repositories — driven by any model through arcllm, safe by default (signed/policy-gated/audited), and extensible through the same arc packages that already exist. v1 is the vertical slice that proves the wedge and is dogfood-usable: the owner works on arc (and other projects) in arctui instead of Claude Code.

## Personas

Primary: **General developer** running a local terminal agent to code on their projects (arctui is targeted at other devs from day one). Secondary: **Extension author** contributing tools/commands/views as arc packages. Tertiary: **Regulated/federal operator** who needs an extensible agent that is signed, policy-gated, and audited. First dogfooder: the arc maintainer, coding on arc itself.

## User Stories

- **US-1**: As a developer, I want to launch a terminal agent in any project folder and have it code there safely, so that I get a Claude-Code-like workflow that is model-agnostic and local.
- **US-2**: As a developer, I want to choose which arc agent and which model (open-source or frontier) drives the session, so that I am never locked to one vendor.
- **US-3**: As a developer, I want the agent loop to be observable and controllable — I see turns, cost, tool activity, approvals, and I can approve/deny/cancel — so that I trust it to finish real work.
- **US-4**: As an operator, I want the folder the agent may touch to be an explicit, revocable trust decision, so that an agent never reads or writes where I did not allow.
- **US-5**: As an extension author, I want to add a tool/command/view as an arc package and have arctui surface it, verified and audited, so that I can grow the terminal agent without forking it.

## Functional Requirements

- **REQ-141** (US-1, Must): arctui SHALL launch via `arc tui`, run the selected agent in the same process/event loop (no subprocess split), and degrade to a no-agent status mode when no agent is resolvable — preserving the existing behavior.
- **REQ-142** (US-4, Must): WHEN arctui is launched against a working directory THEN it SHALL present a folder-trust prompt and grant the agent read/write access to that folder ONLY on confirmation; the grant SHALL be **session-scoped** (in-memory `allowed_paths`, not silently persisted to `arcagent.toml`), SHALL be emitted as an audit event, and the agent's fixed workspace and `protected_paths` denials SHALL remain in force.
- **REQ-143** (US-2, Must): arctui SHALL enumerate agents from the arc roster (`team_root/*/arcagent.toml`) and let the operator select which agent to drive; WHERE no agent exists, it SHALL offer `arc agent create` rather than silently entering no-agent mode.
- **REQ-144** (US-2, Must): arctui SHALL let the operator select and switch the arcllm model in-session — including local open-source backends reachable via a localhost OpenAI-compatible `base_url` (Ollama, vLLM) — and the picker SHALL surface each model's tool-capability so a non-tool-capable model is not silently chosen to drive a coding loop.
- **REQ-145** (US-3, Must): arctui SHALL drive the agent through arcrun and render the live loop from its event stream: streaming assistant output, tool activity (start/end/error), turn count, per-turn and cumulative cost/tokens (from `TurnEndEvent`), and halt/cancel/error outcomes with their reason.
- **REQ-146** (US-3, Must): WHEN a tool call requires approval THEN arctui SHALL present an approval prompt wired to arc's real approval path (`arcstore.approvals` / the agent `approval_provider`), polling the asynchronous out-of-band store, and SHALL honor grant/deny/timeout; a denied or timed-out call SHALL NOT execute.
- **REQ-147** (US-3, Should): arctui SHALL let the operator cancel a running loop from the terminal (kill-switch); an in-flight tool write MAY complete before cancellation takes effect (documented limitation).
- **REQ-148** (US-1, Must): The coding capability SHALL be delivered as a tuned arcagent (bash/tools built in, tuned system prompt + identity), NOT baked into arctui; arctui SHALL run whichever agent is selected against the trusted folder.
- **REQ-149** (US-5, Must): arctui SHALL surface arc's existing extension contributions (tools, skills, modules, MCPs, memory) with no new mechanism, and SHALL add a TUI-side contribution seam for at least one new kind (a slash-command or a view) discovered through the existing `CapabilityLoader`.
- **REQ-150** (US-5, Must): WHEN a third-party TUI contribution (view or command) is loaded THEN arctui SHALL verify it through arc's existing signed-artifact + TOFU capability gate before load; v1 SHALL ship first-party contributions and accept a third-party contribution ONLY when signed and operator-approved at the personal tier, refusing it above personal (the unsigned-floor already enforces this).
- **REQ-151** (US-5, Must): v1 SHALL prove the extension seam end-to-end by installing and running ONE real example extension.
- **REQ-152** (US-1, Should): arctui SHALL consume arc's render-agnostic event/telemetry/store layer (arcrun `StreamEvents`, `arcstore`, `arctrust` audit) and SHALL NOT import arcui or depend on Starlette.
- **REQ-153** (US-4, Must): arctui SHALL hold no secrets in code or config (model/provider credentials resolved through arcllm/vault), and SHALL validate the folder path, agent id, and model id at the trust boundary before use.

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-141, REQ-142, REQ-143, REQ-144, REQ-145, REQ-146, REQ-148, REQ-149, REQ-150, REQ-151, REQ-153 |
| Should | REQ-147, REQ-152 |
| Could | _(none)_ |
| Won't (v1) | Public package registry/marketplace UI; OS-level sandbox isolation for untrusted views; the deeply-tuned "beats pi" SOTA coding agent (v1 ships a usable preset); token-live streaming if it requires an arcrun/arcllm change (see Open Questions); repo-local `.arc` agents. |

## Success Metrics

(1) The maintainer uses arctui to work on arc (and other repos) in place of Claude Code — the dogfood test. (2) A coding session drives the agentic arcrun loop end-to-end to completion with visible turns/cost/approvals and no derail. (3) A local open-source model (Ollama) drives a real coding turn with zero new provider code. (4) One example extension is installed and runs through the signed capability gate. (5) Per-package quality gates hold (ruff, mypy --strict, tests; arctui stays a thin interface — no arcui/Starlette dependency).

## Risks and Constraints

- **Block-at-end streaming.** arcrun currently emits final assistant content as one block, not token-live (`streams.py:379-392`) — the "live typing" feel may be limited without an arcrun/arcllm change. Decide in the SDD whether v1 accepts block-at-turn or invests in token-live.
- **Async out-of-band approvals.** Approvals are operator-signed and resolved out of band via `arcstore.approvals`; the TUI modal must POLL the store, never block a synchronous callback (ASI09).
- **OSS tool-capability varies.** Some open-source models have `supports_tools=false` and cannot drive the coding loop; the model picker must surface this (REQ-144).
- **In-process view isolation is infeasible.** A Textual widget is arbitrary code; arc rejected in-process sandboxing (ADR-017C). The posture is sign + operator-approve + audit, not runtime confinement (REQ-150).
- **Thin-interface constraint.** arctui must reuse arc's package/capability/hook system and the render-agnostic event/store layer; it must not invent a parallel extension system or couple to arcui. Loops via arcrun, models via arcllm, agents via arcagent — respect the boundaries. Compliance: `.claude/steering/tech.md#compliance` (no arctui-specific mandate file; OWASP baseline auto-applies).

## Open Questions

- Token-live streaming in v1 (accept block-at-turn, or invest in an arcrun/arcllm streaming change)? — recommended: accept block-at-turn for v1, track live-tokens as a follow-on.
- Folder-trust persistence: session-scoped (recommended by research — avoids stale grants) vs. remembered-per-folder like Claude Code. REQ-142 chose session-scoped; revisit if daily use makes re-prompting annoying.
- Extension manifest: reuse the arc package / arcskill.hub manifest as-is vs. a light arctui descriptor — resolved in the SDD (research recommends reuse, no new manifest).
- Repo-local `.arc` agents as a second roster source — deferred; not in v1.
