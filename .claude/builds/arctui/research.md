# arctui — build & deepen notes

The `/build` and `/deepen` output for this feature: research insights, architecture diagrams,
component lists, risk registers, open questions and handoff notes. Verbatim, in original order.

**Decisions from this build:** D-482–D-488 (7 total) — see [`.claude/decisions-log.md`](../../decisions-log.md).

---

## arctui — Terminal Agent Interface — Build Decisions (2026-07-24)

**Phase**: build | **Status**: complete | **Total decisions**: 7 (5 user, 2 auto-applied)
**ID range**: D-482 to D-488
**Priority framework**: simplicity → modularity → security → scalability

#### Summary
arctui is a thin TERMINAL INTERFACE over arc's existing systems, not a new platform. It surfaces and drives what arc already provides (agents, capabilities/tools/skills/modules, memory, MCPs, arcrun loops, arcllm models, hooks); extensibility lives in arc and is rendered by the TUI (inspiration: pi's tui package, a rendering framework separate from its coding-agent). v1 = the reliable working agentic loop through arcrun, daily-usable like Claude Code on real folders. Because it is a thin interface, the categories not listed below INHERIT arc's existing infrastructure rather than defining new mechanisms: Data Model (no new store — uses arcagent.toml, the ~/.arc agent layout, and arc session/transcript stores; trusted folders persist in the agent toml allowed_paths), API (no new network API — drives arcagent/arcrun in-process), Observability (arc OTel + the arcrun event stream rendered in the activity pane), Audit (arctrust audit — tool calls, approvals, and folder-trust grants emit audit events), Integration (models via arcllm, MCPs via arc's MCP support — no new external services), Performance (in-process Textual; arc cold-start/loop budgets), Testing (arc pytest standard; existing arctui unit+smoke tests), Deployment (arc packaging; the existing `arc tui` entry point).

#### Auto-Applied (Compliance Mandates)
| ID | Category | Decision | Mandated Answer | Citation |
|---|---|---|---|---|










#### Open Questions
- Extension package/manifest FORMAT: reuse the arc package layout as-is, or a lighter 'arctui extension' manifest? How much of the hook surface already exists (capabilities/modules/ExtensionPoint/arcskill.hub) vs must be built (TUI-view hooks, slash-command hooks, install/discovery flow)?
- Trust/sandbox model for third-party TUI VIEWS specifically — a custom Textual widget is arbitrary in-process code; tools/skills have a trust path, views are new.
- Which open-source model backends first (Ollama / vLLM / llama.cpp) and the in-session model-switch UX.
- What 'reliable arcrun loops' concretely needs beyond arcrun today (checkpoint/resume UX, derail detection, cost/turn caps surfaced in the TUI).
- Repo-local .arc agent vs global ~/.arc agent — how agent selection spans both (D-483/D-484 chose folder-trust on a global agent; repo-local remains an option).
- Relationship to arcui (web): share components/telemetry or stay fully separate?

#### Related Solutions
- Internal prior art: arc capability/module system; arcagent.extension.ExtensionPoint families (select-one/select-many); arcskill.hub (skill-marketplace connector); existing arctui package (~1700 LOC Textual — transcript/activity/input, `arc tui`, graceful no-agent mode).
- External references: pi tui + coding-agent (github.com/earendil-works/pi), pi.dev/packages, Claude Code.
- Brainstorm: .claude/brainstorms/2026-07-24-arctui-terminal-agent.md


#### Research Insights (Deepened 2026-07-25)

**Deepening summary.** Six parallel Explore agents researched the open questions against the arc codebase + pi/external refs. Headline: **arctui v1 is largely INTEGRATION of existing arc primitives, not new subsystems.** Nearly everything the vertical slice needs already exists (arcrun reliability, arcllm OSS backends, capability/extension + signing/TOFU, agent roster, arcstore approvals, arctrust audit); several TUI pieces are even built-but-unwired. Key new risk surfaced: streaming is currently block-at-end (not token-live), and the built approval modals aren't connected to the real approval store.

**Extension surface (D-482 / D-486).** Arc already ships the full agent-side contribution system arctui only RENDERS: `@tool/@hook/@background_task/@capability` decorators, `CapabilityLoader` (scan-roots + signing/TOFU gate), the `agent:*` module-bus (arctui already subscribes, `app.py:127`), `arcskill.hub` (signed Sigstore/Rekor marketplace), MCPs (config-only). The ONLY genuinely-new seams are UI-side: (a) a view/widget registry (compose is hardcoded, `app.py:92`), (b) TUI-only slash-commands merged into `SlashCommandCompleter` (`command_completer.py:22`), (c) stackable autocomplete providers, (d) wiring the dead `[tui.theme]` override. **Recommendation (Simplicity→Modularity→Security): no new manifest** — ship TUI contributions as scan-many `@capability` units discovered through the existing `CapabilityLoader`, reusing one trust model; add a thin declarative view/theme registry (dataclasses + `[tui]` config keys); reuse `arcskill.hub` for anything installable. Refs: `extension/{point,families,select}.py`, `capabilities/capability_loader.py:84,190,323-356`, `tools/_decorator.py:134-270`, `arcskill/hub/config.py`.

**Third-party VIEW sandbox (Security).** A Textual widget is arbitrary in-process Python; arc already rejected in-process AST/RestrictedPython sandboxing (ADR-017C, 3 CVEs). **Realistic posture = provenance + operator-approval + audit, not runtime confinement**: a view rides the exact SPEC-047/033 signed-`.arcsig` + TOFU + tier-floor gate (`require_signature` at enterprise/federal, DID-key-pinned, BYO refused above personal). **v1: first-party views only** (builtins root is trusted); third-party views allowed only signed + operator-allowlisted at personal, blocked above by the unsigned-floor. Defer any untrusted-view marketplace + OS isolation (Firecracker) to a later spec. Edge: malicious widget reading/exfiltrating the transcript → deny view-code egress; hanging `render()` → mount with timeout, degrade to Null view.

**arcllm OSS backends + model switch (D-482, No-lock-in).** 17 providers ship; **Ollama, vLLM, HF-TGI already present, and any local OpenAI-compatible `base_url` works TODAY with zero new code** (`config.py:55` allows `http://localhost`). Recommend **Ollama first** (zero-config, no key), **vLLM second** (throughput + endpoint pool). In-session switch: no `set_model` today — arctui updates `config.llm.model`, closes the old model (`agent.py:897`), sets `self._model=None`, lets `_ensure_model` rebuild (close the old httpx pool). Edge: real incremental SSE only on the OpenAI-wire adapters (Ollama/vLLM inherit it; Anthropic/base yield one block); per-model `supports_tools` gate hard-fails tool use on some OSS models (e.g. `deepseek-r1`).

**arcrun loop reliability (D-485, reliable-loops principle).** arc ALREADY has: turn/token/cost breakers + runaway-loop + error-cascade detection (`react.py:50-96`), cancel/kill-switch (`loop.py:284-304`), turn-boundary checkpoint/resume (`checkpoint.py:99-123`), HITL approval pause (`react.py:164-179`), per-tool timeout (`executor.py:87-104`), steer injection, typed StreamEvents. **v1 = SURFACE these** in the terminal (render turn count, per-turn cost/token deltas the TUI derives, tool activity, approvals, halts with reason, cancel). Gaps (mostly defer): no LLM-level retry/backoff (belongs to arcllm; TUI shows no "retrying"), no semantic-derail detection, no mid-tool-write resume, cost breaker is best-effort/priced-only (label it). Edge: cancel is checked between turns/pre-dispatch only — an in-flight write completes (tools must poll `ToolContext.cancelled`).

**Agent discovery / folder-trust (D-483 / D-484).** An agent = any dir with `arcagent.toml`; `team_roster.list_team()` globs `team_root/*/arcagent.toml`; `~/.arc` is the config root; **no repo-local `.arc` agent concept exists** today. arctui `entry.py:64-90` currently does a CWD-only single-agent load — replace with a roster picker over `arc_home()/team` (or `--team-root`). Folder-trust (D-484) coexists cleanly with the fixed workspace: `resolve_workspace_path` checks the workspace first, then `allowed_paths` as additional roots (`_validation.py:274-291`), and `protected_paths` still overlay-deny. **Recommend a SESSION-SCOPED `allowed_paths` grant** (in-memory) over persisting to `arcagent.toml` (avoids stale grants); prune non-existent dirs on load. Repo-local later = just a second roster source (scan `cwd/.arc/*/arcagent.toml`), no engine change. Edge: empty roster → offer `arc agent create`; CWD == a protected_path → warn (protection wins).

**arctui gap vs v1 + arcui relationship.** HAVE: in-process agent + `arc tui` + no-agent mode, streaming transcript (StreamEvents), activity pane (module-bus), input composer + slash autocomplete, theming, slash dispatch. GAPS: approval modals are **built in `prompts.py` but never wired** to `app.py` or the real approval path (`arcstore.approvals.ApprovalStore` + `HumanGate`/`approval_provider`); no folder-trust launch gate; no agent/model picker (hardcoded `arcagent.toml`, session `tui:main`); `TurnEndEvent` totals + DENY/error outcomes ignored; streaming is block-at-end (`streams.py:379-392`), not token-live. **arctui↔arcui recommendation (Simplicity→Modularity): SHARE the render-agnostic event/telemetry/store layer** (arcrun StreamEvents, arcstore, arctrust audit — all exposed via duck-typed seams with no arcui import) but **keep render layers fully separate** (Textual vs React can't share widgets; coupling would drag in Starlette). Smallest v1 additions: (1) wire `ApprovalModal` → `ApprovalStore`/`approval_provider` (poll the async out-of-band store, handle timeout/deny); (2) folder-trust launch gate mirroring `arcui/routes/trust.py` over arctrust; (3) render `TurnEndEvent` (turns/cost/tokens) + outcomes; (4) agent/model picker modal. Refs: `arctui/{app.py:152-176,242-336,prompts.py:37-215,entry.py:64-107}`, `arcstore.approvals`, `arcagent/tools/approval_policy.py:54-130`, `arcui/routes/{approvals,trust}.py`.

**New risks for /specify.** (1) Block-at-end streaming undercuts the "live" coding feel — decide whether v1 needs token-live streaming (an arcrun/arcllm change) or accepts block-at-turn. (2) Async out-of-band approvals mean the TUI modal must POLL the store, not block a callback. (3) `supports_tools=false` OSS models silently can't drive the coding loop — the model picker must surface tool-capability.

---
