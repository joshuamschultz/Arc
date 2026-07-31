# Implementation Plan: arctui — Terminal Agent Interface (v1)

## Context References

- **PRD:** [PRD.md](./PRD.md) · **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)

> v1 is mostly WIRING existing arc primitives into the existing `packages/arctui` Textual app. Domain tags: `{test, ui, backend, auth, infra, mixed}`. TDD: each `(red)` test task precedes its `(green)` implementation.

## Phase 1: Foundation (select + scope the agent)

- **T-764** (red): Roster + picker tests
  - domain: test · Components: COMP-015 · Requirements: REQ-143
  - Acceptance: tests assert arctui enumerates agents via `team_roster.list_team` and selects one; empty roster surfaces an `arc agent create` affordance rather than silent no-agent; fail for the right reason (feature absent).
- **T-765** (green): Roster enumeration + agent picker
  - domain: ui · Components: COMP-015 · Requirements: REQ-141, REQ-143
  - Acceptance: `entry.py`'s CWD-only load is replaced by roster enumeration (`arc_home()/team` or `--team-root`) + a picker; chosen `arcagent.toml` loads; no-agent fallback preserved; T-764 green.
- **T-766** (red): Folder-trust gate tests
  - domain: test · Components: COMP-016 · Requirements: REQ-142
  - Acceptance: tests assert an untrusted folder is denied until confirmed; on confirm a **session-scoped** (in-memory, not persisted to toml) `allowed_paths` grant is applied; an audit event is emitted; workspace + `protected_paths` denials still hold.
- **T-767** (green): Folder-trust gate + session grant + audit
  - domain: auth · Components: COMP-016 · Requirements: REQ-142
  - Acceptance: launch prompts folder-trust; confirmation grants session-scoped `allowed_paths` (via the builtin runtime contextvar), emits `arctrust` audit; never writes `arcagent.toml`; T-766 green.
- **T-768** (red): Model picker + switch tests
  - domain: test · Components: COMP-017 · Requirements: REQ-144
  - Acceptance: tests assert the picker lists arcllm models incl. an Ollama/vLLM localhost entry, surfaces `supports_tools`, and on switch the old model + httpx pool close and `_ensure_model` rebuilds.
- **T-769** (green): Model picker + in-session switch (incl. local OSS)
  - domain: backend · Components: COMP-017 · Requirements: REQ-144
  - Acceptance: in-session model switch works for Ollama/vLLM via localhost `base_url` (zero new provider code); tool-capability shown; old model closed; T-768 green.

## Phase 2: Core (the reliable working loop)

- **T-770** (red): Loop-render tests
  - domain: test · Components: COMP-018 · Requirements: REQ-145
  - Acceptance: tests assert the renderer consumes `StreamEvents`/`TurnEndEvent` and shows turns, per-turn + cumulative cost/tokens, tool start/end/error, and `loop.completed`/`loop.cancelled` reason.
- **T-771** (green): Loop renderer (turns/cost/tokens/activity/outcomes)
  - domain: ui · Components: COMP-018 · Requirements: REQ-145
  - Acceptance: transcript + activity panes render the full loop signal from the event stream; T-770 green.
- **T-772** (red): Approval-bridge tests
  - domain: test · Components: COMP-019 · Requirements: REQ-146
  - Acceptance: tests assert the approval modal polls `arcstore.approvals` (async out-of-band), and a denied/timed-out call does NOT execute; secrets from `SecretInputModal` never enter history.
- **T-773** (green): Wire approval modals to the real approval path
  - domain: backend · Components: COMP-019 · Requirements: REQ-146
  - Acceptance: the built `prompts.py` modals connect to `arcstore.approvals`/`approval_provider`; poll + grant/deny/timeout honored; T-772 green.
- **T-774** (green): Cancel / kill-switch
  - domain: ui · Components: COMP-020 · Requirements: REQ-147
  - Acceptance: a key binding calls `RunHandle.cancel(caller_did, reason)`; `loop.cancelled` rendered; in-flight-write limitation documented.
- **T-775** (green): First-party coding agent preset
  - domain: infra · Components: COMP-021 · Requirements: REQ-148
  - Acceptance: a tuned coding arcagent (bash/tools + coding system prompt + identity via arcprompt) is creatable through `arc agent create`/blueprint and runnable by arctui; the coding smarts live in the agent, not the TUI.

## Phase 3: Integration (extensibility seam)

- **T-776** (red): Contribution seam + trust-gate tests
  - domain: test · Components: COMP-022, COMP-023 · Requirements: REQ-149, REQ-150
  - Acceptance: tests assert a TUI contribution (view/command) is discovered via `CapabilityLoader`; an unsigned third-party contribution is refused above personal tier and a signed one is accepted after operator approval; first-party (builtins) load without prompt.
- **T-777** (green): TUI contribution seam (views/commands/autocomplete/theme)
  - domain: ui · Components: COMP-022 · Requirements: REQ-149
  - Acceptance: a view/widget registry feeds `app.compose()`, TUI slash-commands merge into `SlashCommandCompleter`, autocomplete providers stack, `[tui.theme]` config is wired; contributions are scan-many `@capability` units (no new manifest); T-776 partially green.
- **T-778** (green): Contribution trust gate
  - domain: auth · Components: COMP-023 · Requirements: REQ-150
  - Acceptance: contributions verified through arctrust signed-artifact + TOFU before load; tier floor enforced; malicious-view mitigations (egress-deny, mount timeout, degrade-to-Null); T-776 green.
- **T-779** (green): One real example extension, end-to-end
  - domain: mixed · Components: COMP-024 · Requirements: REQ-151
  - Acceptance: a real example extension (slash-command or simple view) is packaged as an arc capability, installed, and runs in arctui through the signed gate — proving the seam.

## Phase 4: Polish & Gates

- **T-780** (red): arctui-does-not-import-arcui architecture guard
  - domain: infra · Components: COMP-025 · Requirements: REQ-152
  - Acceptance: `tests/architecture/test_no_arctui_imports_arcui.py` AST-scans `packages/arctui/src` and fails on any `arcui` import (or Starlette); passes today; proven to fail on a probe import.
- **T-781** (green): Secrets + input-boundary hardening
  - domain: backend · Components: COMP-026 · Requirements: REQ-153
  - Acceptance: no secrets in arctui code/config (keys via arcllm/vault); folder path, agent id, model id validated at the boundary.
- **T-782** (refactor): Close quality gates + dogfood
  - domain: mixed · Components: all · Requirements: REQ-141…REQ-153
  - Acceptance: ruff clean, mypy --strict clean, arctui suite green, coverage ≥ 80% line / 75% branch on touched code; arctui stays a thin interface (no arcui/Starlette dep); dogfood check — arctui runs a coding turn on the arc repo through a local + a frontier model.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-141 | T-765 |
| REQ-142 | T-766, T-767 |
| REQ-143 | T-764, T-765 |
| REQ-144 | T-768, T-769 |
| REQ-145 | T-770, T-771 |
| REQ-146 | T-772, T-773 |
| REQ-147 | T-774 |
| REQ-148 | T-775 |
| REQ-149 | T-776, T-777 |
| REQ-150 | T-776, T-778 |
| REQ-151 | T-779 |
| REQ-152 | T-780 |
| REQ-153 | T-781 |
| (all) | T-782 |

## Open Questions

- Token-live streaming (arcrun/arcllm change) — v1 accepts block-at-turn; revisit post-v1.
- Coding preset as an `arc` blueprint vs a documented `arc agent create` recipe (T-775 picks one).
- Session-scoped folder-trust — revisit persistence if re-prompting proves annoying in daily use.
