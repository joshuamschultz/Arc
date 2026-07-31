# Specification: arctui — Terminal Agent Interface (v1)

**Feature:** `SPEC-058-arctui-terminal-agent`
**Created:** 2026-07-25

## Status

| Doc | Status | Last Update |
|---|---|---|
| PRD | approved | 2026-07-25 |
| SDD | draft | 2026-07-25 |
| PLAN | draft | 2026-07-25 |

## Summary

A terminal *interface* to arc — run and control an arc agent from the command line, model-agnostic (open-source or frontier via arcllm) and local-first, with arc's trust guarantees. Not a new agent or extension system: it surfaces what arc already has. v1 is the vertical slice that's daily-usable like Claude Code on real folders, proven by dogfooding on arc. Research (deepen) confirmed v1 is mostly WIRING existing primitives.

## Steering References

- Product: [`../../steering/product.md`](../../steering/product.md) · Tech: [`../../steering/tech.md`](../../steering/tech.md) · Structure: [`../../steering/structure.md`](../../steering/structure.md)

## Workflow Provenance

- **Brainstorm:** [`../../brainstorms/2026-07-24-arctui-terminal-agent.md`](../../brainstorms/2026-07-24-arctui-terminal-agent.md)
- **Build decisions:** `.claude/decisions-log.md` → "arctui — Terminal Agent Interface" — D-482 (thin interface over arc), D-483 (coding = tuned arcagent), D-484 (folder-trust → allowed_paths), D-485 (v1 = reliable arcrun loop), D-486 (extensions = arc units + one real extension), D-487/488 (OWASP baseline).
- **Research (deepened 2026-07-25):** six Explore reports on the open questions — appended under the arctui section of the decisions log.

## Scope

- **In (v1):** roster agent picker; folder-trust launch gate (session-scoped `allowed_paths`); model picker + in-session switch incl. Ollama/vLLM; live arcrun loop rendering; wired approvals; cancel; a tuned coding-agent preset; a TUI contribution seam through the existing signed capability gate proven by one real extension; the arcui-boundary guard.
- **Out (v1):** marketplace UI, OS-level view isolation, the SOTA-tuned coding agent, token-live streaming (if it needs an arcrun change), repo-local `.arc` agents.

## Key Design Facts

- arctui reuses arc's render-agnostic seams (arcrun `StreamEvents`, `arcstore`, `arctrust`) and **must not import arcui** (guarded).
- Coding lives in a tuned arcagent, not the TUI. Extensibility = arc's existing capability/signing/TOFU system; the only new bits are UI-side seams (views/commands/autocomplete/theme).
- Ollama/vLLM work today via a localhost OpenAI-compatible `base_url` — zero new provider code.

## Phase Notes

### Phase 1 — in progress (2026-07-25)
- **T-764 DONE + verified** (test, REQ-143): `tests/unit/test_roster.py`.
- **T-765 DONE + verified** (ui, REQ-141/143): `roster.py` — `list_agents(~/.arc/team)`, `select_agent`, `resolve_agent` (named / single-auto / ambiguous / empty / unknown). `entry.py` rewired — `_resolve_agent_config(args, cwd, team_root)` replaces the CWD-only load: roster + `--agent`/`--team-root`, single-auto, cwd-arcagent.toml fallback, clear "pass --agent"/"arc agent create" messages; `main(args)`/`_tui_handler` thread argv. **83 arctui tests pass**, ruff clean, roster.py/entry.py + tests mypy-strict clean. All in `packages/arctui` (uncommitted).
  - *Deferred within T-765:* the interactive Textual picker SCREEN for the `ambiguous` case (v1 currently asks for `--agent`); add as a first-screen modal over `resolve_agent`.
- **T-766/T-767 DONE + verified** (folder-trust, REQ-142): `trust.py` — `folder_needs_trust` + `grant_folder` (session-scoped, in-memory append to `tools.policy.allowed_paths`, idempotent, never writes the toml, protected_paths untouched). `entry.py:_apply_folder_trust` prompts on launch (TTY only; non-interactive → no grant), grants before `ArcAgent` startup. 88 arctui tests pass; end-to-end flow verified (resolve → load → trust → toml unchanged). *Refinement (T-782):* emit a formal `arctrust` audit event for the grant (currently structured-logged).
- **Makefile:** arctui added to the `install` target (was missing → Textual absent). `make install` now pulls arctui + textual 1.0.0.
- **BASIC LAUNCHABLE TUI reached** (T-764–767 + existing transcript/activity/input loop). `arc tui` launches, picks an agent from the roster (or `--agent`), trusts the cwd, and drives it through arcrun. Remaining tasks are enhancements: T-768/769 (in-session model switch; model already comes from arcagent.toml), T-771 (turn/cost rendering), T-773 (approval UI; personal-tier auto-runs tools), Phase 3 (extension seam), Phase 4 (polish + audit-event + arcui-boundary guard).
- **Resume at:** T-768/769 (model picker) → arcllm supports Ollama/vLLM today; switch = update `config.llm.model`, close old model+pool, `_ensure_model` rebuild.

### Phase 3 — attach-or-serve gateway client DONE + verified (2026-07-26)
**Architecture pivot (per Josh):** arctui is a *viewpoint*, not an agent owner. The
gateway (`arc ui start` / `arc team serve`) is the single `ArcAgent` owner; every
surface (arcui, telegram, slack, arctui) attaches. This kills the WORM
single-writer collision (arctui used to construct its own `ArcAgent` → fought
`arc ui start` for `audit-chain-<agent>.jsonl`).

- **New modules (all mypy-strict clean, TDD):**
  - `transport.py` — `TurnEvent` + `ChatTransport` Protocol (the render seam; app no longer touches `ArcAgent`).
  - `gateway_client.py` — `GatewayChatClient`: WS client of `/ws/chat/{agent_id}`. Handshake `{"token"}`→`{"type":"ready"}`, drive `{"type":"message","text"}`, block-at-turn reply `{"type":"message","from":"agent"}` (web adapter is send-only). 11 unit + 1 live-websockets round-trip test.
  - `serve.py` — `ensure_gateway`: **attach-or-serve** in one call. Probe `/api/health`; attach if reachable (token via `--token`, or the loopback token `arc ui start` now persists to `~/.arc/ui/viewer-token` 0600); else spawn detached `arc ui start --no-browser --team-root --viewer-token --port`, wait for health, attach. 8 unit tests.
- **Rewired:** `entry.py` — `resolve agent → ensure_gateway → GatewayChatClient → app`; supports `--agent/--team-root/--host/--port/--url/--token`. `app.py` — renders `transport.send_turn(text)` (dropped the in-process `agent.run` + module-bus bridge); app.py is now mypy-strict clean. `arccli ui.py` — persists the loopback viewer token so a same-user `arc tui` auto-attaches.
- **Modernization (first pass):** header status line shows attach context (`◆ agent · gateway · turns N`).
- **DELETED:** `trust.py` + `test_trust.py` — in-memory config mutation doesn't fit the attach model (the served agent, in another process, owns its file policy). Folder-trust must be re-homed as a **spawn-time allowed-path grant** to the spawned gateway when the coding preset lands (revisits D-484). Roster (`roster.py`) retained.
- **Verified:** 107 arctui tests + 14 arccli-ui tests pass; ruff clean; mypy-strict clean on transport/gateway_client/serve/entry/app; `arc tui` command registered; spawn argv correct + detached; real `websockets` round-trip green.

### Phase 3 remaining — the deep agentic UI (NEXT)
- **Inline live tool calls (Claude-Code-style):** blocked on transport — `/ws/chat` is send-only (block-at-turn), so tool events never reach the client. Fix = stream `tool_call`/token frames over `/ws/chat` (web-adapter change, shared surface — needs its own tests) OR overlay by polling `arcstore` run timeline. Design decision for Josh.
- **Observe panels (arcllm metrics · traces · sessions · run history/details):** source from the gateway's observe API (`/api/traces`, `/api/runs`, `/api/runs/{id}/timeline`, `/api/agents/{id}/sessions`) — arctui already holds `base_url` + token. New API-client module + Textual panels.

### Findings (2026-07-25)
- **Env gap (fixed this session):** arctui was NOT installed (absent from the Makefile `install` target), so its pinned `textual>=0.80,<2` was missing → Textual tests couldn't collect. Fixed via `uv pip install -e packages/arctui` (textual 1.0.0). **Add arctui to the Makefile install target** (and consider `_CANONICAL_PACKAGES`) as part of T-782.
- **Pre-existing arctui-src mypy debt (out of scope for T-765, flag):** `--strict` reports ~21 errors across `app.py`/`input_composer.py`/`prompts.py`/`activity.py`/`transcript.py`. arctui is NOT in `make typecheck`, so it's untracked debt. New code (roster.py/entry.py) is clean. Fold a decision into T-782: add arctui to typecheck + pay down, or leave.
- **Build context caveats:** work on shared branch `feat/quick-deploy` (owner's uncommitted arccli scaffold changes — never touch `arccli/commands/agent/*`). `.claude/` is gitignored → spec docs on-disk only. `uv pip install -e` did not modify `uv.lock`.

## Learnings

_(none yet)_

## Open Questions

- Token-live vs block-at-turn streaming (v1 accepts block-at-turn).
- Session-scoped vs remembered folder-trust (v1 = session-scoped).
- Coding preset as an `arc` blueprint vs a documented `arc agent create` recipe.
