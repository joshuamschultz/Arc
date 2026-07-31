# Solution Design Document: arctui — Terminal Agent Interface (v1)

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **Build decisions:** `.claude/decisions-log.md` → arctui (D-482…D-488 + Research Insights)
- **Tech / structure:** [.claude/steering/tech.md](../../steering/tech.md) · [.claude/steering/structure.md](../../steering/structure.md)

## Overview

arctui v1 is delivered almost entirely by WIRING existing arc primitives into the already-present Textual app (`packages/arctui`, ~1,700 LOC), plus a small first-party TUI contribution seam. The research (deepen, 2026-07-25) confirmed that arcrun (loop + reliability + event stream), arcllm (17 providers incl. Ollama/vLLM), the capability/extension + signing/TOFU system, the agent roster, `arcstore` approvals, and `arctrust` audit already exist. arctui consumes arc's render-agnostic seams (arcrun `StreamEvents`, `arcstore`, `arctrust`) and never imports arcui. Coding capability is a tuned arcagent the TUI runs, not TUI logic.

## Architecture

arctui sits above arcagent/arcrun/arcllm/arcstore/arctrust as a pure terminal render+control layer (per `structure.md` dependency direction: UI depends on the harness, never the reverse; arctui must not import arcui). The run flow: launch → resolve roster + pick agent (COMP-015) → folder-trust gate grants a session-scoped `allowed_paths` (COMP-016) → pick/confirm model incl. local OSS (COMP-017) → drive the agent through arcrun, rendering the event stream (COMP-018) with approvals (COMP-019) and cancel (COMP-020). Extensibility is arc's existing capability system surfaced by the TUI, plus one new TUI-side contribution kind loaded through the same signed/TOFU gate (COMP-022/023), proven by one example extension (COMP-024). Error handling follows `tech.md#error-handling-pattern` (typed exceptions + audit).

## Components

### COMP-015: Agent roster + picker (arctui)
**Responsibility:** Replace `entry.py`'s CWD-only single-agent load with enumeration via `arcgateway.team_roster.list_team(team_root=arc_home()/"team"` or `--team-root)`; present a picker keyed by roster entry; load the chosen `arcagent.toml`. Empty roster → offer `arc agent create`, not silent no-agent.
**Dependencies:** arcgateway.team_roster, arctrust.paths.arc_home, existing arctui entry/app.
**Inputs/Outputs:** launch args/env → selected agent root + loaded config. **REQ-143.**

### COMP-016: Folder-trust gate (arctui)
**Responsibility:** On launch against a working dir, prompt "is this folder safe?"; on confirm, apply a **session-scoped** `allowed_paths` grant to the agent's builtin runtime (in-memory override of the `allowed_paths` contextvar — `builtins/capabilities/_runtime.py`), never persisting to `arcagent.toml`; emit an `arctrust` audit event. Fixed workspace and `protected_paths` denials remain in force (`_validation.py:274-291`).
**Dependencies:** arcagent runtime allowed_paths wiring, arctrust.audit, `_validation.resolve_workspace_path` semantics.
**Inputs/Outputs:** cwd + confirmation → session grant + audit event. **REQ-142.** Threat: LLM06 excessive agency, ASI03.

### COMP-017: Model picker + in-session switch (arctui)
**Responsibility:** List arcllm providers/models incl. local OSS (Ollama/vLLM via localhost `base_url`), showing each model's `supports_tools`; on switch, update `config.llm.model`, close the old model + its httpx pool (`agent.py:897`), set `self._model=None`, let `_ensure_model` rebuild.
**Dependencies:** arcllm providers/registry, arcagent.core.model_manager / `_ensure_model`.
**Inputs/Outputs:** selection → active model rebound. **REQ-144.** No-lock-in pillar.

### COMP-018: Loop renderer (arctui)
**Responsibility:** Consume the arcrun `EventBus`/`StreamEvents` and render: streaming assistant output, tool start/end/error, turn count, per-turn + cumulative cost/tokens (derived from `TurnEndEvent`, `streams.py:83-110`), and `loop.completed`/`loop.cancelled` outcomes with reason. Extends the existing transcript + activity panes (which already consume the bus).
**Dependencies:** arcrun streams/events (already subscribed at `app.py:127`).
**Inputs/Outputs:** event stream → rendered panes. **REQ-145.** Reliability pillar (surface, don't rebuild).

### COMP-019: Approval bridge (arctui)
**Responsibility:** Wire the built-but-unwired `prompts.ApprovalModal`/`SudoModal`/`ClarifyModal`/`SecretInputModal` to arc's real approval path — consume `arcstore.approvals.ApprovalStore` and/or set the agent `approval_provider` (`arcagent/tools/approval_policy.py:54-130`). Poll the asynchronous out-of-band store (operator-signed); handle grant/deny/timeout; a denied/timed-out call does NOT execute. Secrets from `SecretInputModal` never enter input history.
**Dependencies:** arcstore.approvals, arcagent approval_provider/HumanGate, existing prompts.py modals.
**Inputs/Outputs:** approval.required event → modal → grant/deny to the loop. **REQ-146.** Threat: ASI09, LLM06.

### COMP-020: Cancel / kill-switch (arctui)
**Responsibility:** Bind a key to `RunHandle.cancel(caller_did, reason)` (`loop.py:284-304`); render `loop.cancelled`. Document that an in-flight tool write may complete (cancel is checked between turns / pre-dispatch).
**Dependencies:** arcrun RunHandle. **REQ-147.**

### COMP-021: Coding agent preset (arcagent)
**Responsibility:** A first-party tuned arcagent — bash/tools already built in, with a coding-tuned system prompt + identity (authored as editable arcprompt/identity content) — shippable via the agent-create/blueprint path. arctui runs it; the coding smarts live here, not in the TUI.
**Dependencies:** arcagent agent scaffold/blueprints, arcprompt (tunable prompts), bash/file tools.
**Inputs/Outputs:** `arc agent create`/blueprint → a coding agent arctui can select. **REQ-148.** Modularity + dogfood pillars.

### COMP-022: TUI contribution seam (arctui)
**Responsibility:** Add the UI-only seams arc lacks: a view/widget registry consumed in `app.compose()` (currently hardcoded, `app.py:92`), a TUI-only slash-command set merged into `SlashCommandCompleter` (`command_completer.py:22`), stackable autocomplete providers, and wiring the currently-dead `[tui.theme]` config. TUI contributions are discovered as scan-many `@capability` units through the existing `CapabilityLoader` — no new manifest.
**Dependencies:** arcagent capabilities (CapabilityLoader, @capability), arctui app/completer/theme.
**Inputs/Outputs:** installed capability units → registered views/commands/theme. **REQ-149.**

### COMP-023: Contribution trust gate (arctui/arctrust)
**Responsibility:** Verify third-party TUI contributions through arc's existing signed-artifact + TOFU gate before load (`capability_loader.py:323-356`, `require_signature` tier floor, DID-key pinning). v1 ships first-party (trusted builtins root) only; a third-party contribution is accepted ONLY signed + operator-approved at personal tier, refused above personal. Malicious-view mitigations: deny view-code egress, mount with a timeout, degrade to a Null view.
**Dependencies:** arctrust verify_artifact/TOFU, CapabilityLoader trust gate. **REQ-150.** Security pillar; ASI04/ASI05.

### COMP-024: Example extension
**Responsibility:** One real example extension (a slash-command or a simple view) packaged as an arc capability, installed and run end-to-end to prove the seam (COMP-022/023).
**Dependencies:** COMP-022, COMP-023. **REQ-151.**

### COMP-025: Event/telemetry sharing + arcui boundary (arctui)
**Responsibility:** arctui consumes arc's render-agnostic layer (arcrun `StreamEvents`, `arcstore`, `arctrust` audit) and MUST NOT import arcui or depend on Starlette. Enforced by an architecture guard test.
**Dependencies:** arcrun/arcstore/arctrust; `tests/architecture`. **REQ-152.** Modularity pillar.

### COMP-026: Secrets + input-boundary hardening (arctui)
**Responsibility:** No secrets in arctui code/config (model/provider keys via arcllm/vault); validate folder path, agent id, and model id at the boundary before use.
**Dependencies:** arcllm/vault, input validation. **REQ-153.** OWASP baseline.

## Data Model

No new persistent store. Trusted folders are **session-scoped in-memory** (not persisted). Agent config remains `arcagent.toml`; sessions/transcript/memory remain under the agent's `~/.arc`-rooted workspace. Audit events (folder-trust grant, approvals, cancel) flow through `arctrust.audit`. In-memory: the active agent handle, active model, session-grant set, registered TUI contributions.

## External Integrations

All intra-repo: arcagent (run/dispatch, config, model_manager, roster via arcgateway.team_roster), arcrun (loop, events, RunHandle), arcllm (providers incl. Ollama/vLLM), arcstore (approvals), arctrust (audit, signing/TOFU), arcprompt (tunable coding prompts for COMP-021). No new external network services. Local OSS models reached via localhost OpenAI-compatible `base_url` (Ollama :11434, vLLM :8000) — zero new provider code.

## Traceability

| Requirement | Components |
|---|---|
| REQ-141 | COMP-015 (+ existing app/entry) |
| REQ-142 | COMP-016 |
| REQ-143 | COMP-015 |
| REQ-144 | COMP-017 |
| REQ-145 | COMP-018 |
| REQ-146 | COMP-019 |
| REQ-147 | COMP-020 |
| REQ-148 | COMP-021 |
| REQ-149 | COMP-022 |
| REQ-150 | COMP-023 |
| REQ-151 | COMP-024 |
| REQ-152 | COMP-025 |
| REQ-153 | COMP-026 |

## Alternatives Considered

New arctui-specific plugin layer (rejected — parallel system; D-482 reuses arc's). Coding baked into arctui (rejected — couples interface to one use case; D-483). Persisted per-folder trust in `arcagent.toml` (rejected for v1 — stale-grant risk; research recommends session-scoped; revisit if re-prompting annoys). Coupling arctui to arcui for shared components (rejected — Textual vs React can't share widgets, would drag in Starlette; share only the event/store layer). In-process view sandbox (rejected — infeasible per ADR-017C; use sign+approve+audit). Token-live streaming in v1 (deferred — requires an arcrun/arcllm change; v1 accepts block-at-turn).

## Threat Surface Mapping (per CLAUDE.md)

- **LLM06 Excessive agency / ASI03 identity:** folder-trust gate (COMP-016) + approvals (COMP-019) + arcrun policy; every action carries caller_did.
- **ASI04 supply chain / ASI05 code execution:** TUI contributions signed + TOFU-gated before load (COMP-023); no in-process eval.
- **ASI09 human-agent trust:** approval prompts show tool + args; deny/timeout blocks execution (COMP-019).
- **LLM10 unbounded consumption:** arcrun breakers (turn/cost/runaway) surfaced in the loop renderer (COMP-018).
- **LLM07 prompt leakage:** no secrets in arctui (COMP-026); coding prompts are arcprompt-managed, editable/auditable.

## Risks and Mitigations

Block-at-end streaming limits the "live" feel — accept for v1, flag token-live as follow-on. Async out-of-band approvals — the modal polls the store, never blocks a callback. OSS `supports_tools=false` — picker surfaces capability (COMP-017). View isolation infeasible — sign+approve+audit posture (COMP-023). Thin-interface drift — the arcui-boundary guard (COMP-025) plus reuse of arc seams keep arctui from growing a parallel system.

## Open Questions

- Token-live streaming: accept block-at-turn for v1 (recommended) or invest now?
- Session-scoped vs remembered folder-trust (v1 = session-scoped).
- Whether the coding preset ships as an `arc` blueprint vs a documented `arc agent create` recipe (resolve in PLAN).
