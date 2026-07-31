# Arc Codebase Inventory — Evidence-Backed (2026-07-25)

Graph: 1672 files, 40279 nodes, 240933 edges (code-review-graph, built at da26aeb on feat/quick-deploy).
Total non-test Python source: 118,417 LOC across 17 packages + arcui web (TS/TSX): ~12,863 LOC.
Test files: 978 `test_*.py` files (8,564 Test nodes in graph). Packages: 18 dirs under `packages/`.

## 1. PACKAGE MAP

| Package | LOC (py) | Purpose | Key entry points | Maturity |
|---|---|---|---|---|
| **arcagent** | 37,855 | The agent runtime: identity, config, tool registry, module bus, blueprints, memory/skill/tool extension points, 19 modules (memory, browser, web, tasks, messaging, planning, policy, proactive, pulse, runcontrol, scheduler, session, skills, slack, telegram, user_profile, voice, workpad). `core/` alone = 6,328 LOC (over the 3,500 CLAUDE.md budget; ADR-004 raised it). | `core/agent.py`, `core/tool_registry.py` (4 transports: native/MCP/HTTP/process), `core/module_bus.py`, `tiers.py` | Solid |
| **arccli** | 11,816 | `arc` console script — agent lifecycle, llm/run/skill/ext/blueprint/team/ui/gateway/init subcommands (see §2). | `src/arccli/main.py`, `commands/` | Solid |
| **arcgateway** | 8,919 | Embedded multi-platform message gateway (SPEC-023): pairing, session queue, executor (NATS/subprocess), fleet, audit. Standalone `arcgateway start` daemon refuses to start at every tier per docs/cli.md — only used embedded via `arc ui start`. | `bootstrap.py`, `executor.py`, `pairing.py`, `session.py` | Partial (embedded path solid; standalone daemon intentionally disabled) |
| **arcgateway-slack / -telegram / -mattermost** | 606 / 1,003 / 662 | Thin per-platform adapter+plugin+config, 4 files each. | `adapter.py`, `plugin.py` | Partial — minimal surface, live but narrow |
| **arcllm** | 9,531 | Provider-agnostic LLM calls: registry, vault, embeddings, trace store/query/retention, PII scrubbing, capabilities. 16 provider TOMLs (anthropic, openai, azure_openai, google, cohere, deepseek, fireworks, groq, huggingface(+tgi), mistral, moonshot, ollama, together, vllm, xai). | `registry.py`, `embeddings.py`, `vault.py` | Solid |
| **arcmas** | 13 | Meta-package only — `pip install arcmas` installs the whole stack. No logic of its own (README: "The meta-package"). | — | Stub (by design, not a code package) |
| **arcmemory** | 7,354 | Dual-speed, 4-store memory (episodic/semantic/procedural/insight in `db.py`), zero-LLM capture (`capture.py`), LLM consolidation (`consolidate.py`, `distill.py`), analogical retrieval (`retrieve.py`, `fusion.py`), hygiene/dedup, arcrun-based agentic consolidation (`react_adapter.py`). | `brain.py` (Protocol), `tools.py`, `retrieve.py` | Solid |
| **arcmodel** | 3 | Explicitly a placeholder — README states "Status: early scaffolding... installs and exports `__version__` only. No public API yet." | — | Stub (confirmed by own docs) |
| **arcprompt** | 640 | Signed, overlay-able, editable system prompts (SPEC shipped 2026-07). | `resolver.py`, `verifier.py`, `catalog.py` | Solid, small/focused |
| **arcrun** | 6,349 | The agentic loop: executor, sandbox, checkpoint/resume, parallel_dispatch, registry (react/code strategies), circuit breaker. | `loop.py`, `executor.py`, `sandbox.py` | Solid |
| **arcskill** | 9,501 | Skill format (SKILL.md + 7 sections), signing, hub connector, improver (GEPA+SkillOpt). NOTE: top-level `src/arcskill` only shows `__init__.py`+`lock.py` at depth-2 — bulk of the 9.5K LOC is in subpackages not enumerated here (improver/, hub/). | `lock.py`, `hub/`, `improver/` (per ROADMAP-PROGRAM SPEC-044) | Solid |
| **arcstore** | 2,793 | Operational storage: approvals, cancellations, ingest, query, records, spool, **tasks.py (697 LOC — Mission Control backend)**. | `tasks.py`, `approvals.py`, `spool.py` | Solid |
| **arcteam** | 4,398 py + 735 ts | Multi-agent messaging: registry, messenger, mentions (SPEC-055), NATS server, crypto, audit. | `team.py`, `messenger.py`, `mentions.py`, `nats_server.py` | Solid |
| **arctrust** | 4,840 | Identity/signing/policy backbone: keypair, signer (Ed25519+FIPS ECDSA), policy pipeline, audit, classification, TOFU, witness (WORM chain), operator key. | `policy.py`, `signer.py`, `audit.py`, `identity.py` | Solid |
| **arctui** | 1,705 | Terminal UI (Textual-based): `app.py`, `activity.py`, `transcript.py`, `input_composer.py`, `command_completer.py`. Single `ArcTUI(App)` class — one main screen, not a multi-screen app. | `app.py` | Partial — functional but thin (SPEC-058 "arctui terminal agent" has a PRD only, no README yet) |
| **arcui** | 9,750 py + 12,863 ts | Web dashboard: Starlette backend (19 route modules), React/TS frontend (12 pages). | `server.py`, `routes/*.py`, `web/src/pages/*.tsx` | Solid |

## 2. FEATURE INVENTORY

**Agent creation & config** — SHIPPED. `arc agent create/build/chat/run/serve/status/config/tools/skills/extensions/sessions/reload/strategies/events` (docs/cli.md). `arcagent.toml` schema: `[agent]`, `[llm]`, `[identity]`, `[vault]`, `[tools.policy]`, `[tools.mcp_servers.<name>]` (config field exists — see MCP below), `[telemetry]`, `[context]`, `[session]`, `[modules.*]`. Blueprints (`arc blueprint list/show/apply/verify/sign`) with 3 packaged presets (personal-assistant, enterprise-ops, federal-analyst).

**CLI surface** — SHIPPED, ~14 command groups: `agent`, `llm`, `run`, `skill`, `ext`, `blueprint`, `team`, `ui`, `init`, `gateway`, `help`/`version`/`quit`. Full table in docs/cli.md (409 lines).

**Web UI (arcui)** — SHIPPED. 12 pages (agents, agent-detail, approvals, arcllm, arcrun, gated-capabilities, knowledge, messages, policy, security, settings, tasks, tools-skills) backed by 19 route modules (`packages/arcui/src/arcui/routes/*.py`): knowledge (13 routes), tasks (6), stats (6), team_pages (6), team_chat (5), observe_run (4), approvals/trust/config (2-3 each), plus WS endpoints (chat_ws, team_ws).

**TUI (arctui)** — PARTIAL. Single-screen Textual app (transcript, input composer, command completion, activity indicator). SPEC-058 has a PRD only, no implementation README found confirming completion status — treat as in-progress/unverified beyond the base chat screen.

**Gateways** — SHIPPED (narrow): Slack, Telegram, Mattermost, each a 4-file adapter+plugin+config (600-1000 LOC). Standalone `arcgateway` daemon is intentionally disabled at every tier; only runs embedded inside `arc ui start` (SPEC-023 pattern).

**Multi-agent (arcteam)** — SHIPPED: DID-keyed registry, signed messenger, channels/teams, mention-scoped activation (SPEC-055, `mentions.py`), NATS transport. **arcmas** is not a multi-agent framework — it's the meta pip-install package (name is misleading).

**Tasks/Mission Control (SPEC-056)** — SHIPPED despite README saying "DRAFT": `arcstore/tasks.py` (697 LOC, atomic claim/status machine), `arcagent/modules/tasks/` (store, capabilities, runtime), `arcui/routes/tasks.py` (6 routes) + `web/src/pages/tasks.tsx`. This is a confirmed case of **spec-status field drift** (memory: project_spec_status_sync_requirement) — code shipped, README status never updated.

**Tools (built-in)** — SHIPPED: read, write, edit, ls, grep, find, bash, store_secret, create_tool, update_tool, create_skill, update_skill, reload (`arcagent/builtins/capabilities/`). Modules add memory/browser/web/messaging/tasks tools.

**MCP** — Arc is **NEITHER an MCP client nor server in practice**, despite scaffolding. `ADR-018-no-mcp-no-migration-no-acp.md` explicitly excludes MCP host support from SPEC-018 scope. Code has a `TransportKind.MCP` enum value (`arcagent/tools/_transport.py:31`) and an `MCPServerEntry` config model + `mcp_servers` dict field (`arcagent/core/config.py:152,194`), but `tool_registry.py` only *mentions* MCP in a docstring ("Supports 4 transports: native, MCP, HTTP, process") with no actual dispatch/session code implementing it. **Verdict: SPEC-ONLY / scaffolded-but-unwired.** SPEC-045 (scale+interop, status ⬜ pending) is the roadmap item that would build real MCP client+server.

**Skills (arcskill)** — SHIPPED: SKILL.md format (frontmatter + 7 sections), signing via arctrust, improver (GEPA trace-reflection + SkillOpt bounded-edit, golden-task gate), hub connector. `arcskill` is an optional supercharger — arcagent runs skills alone via `NullSkillAdapter`.

**Memory (arcmemory)** — SHIPPED: 4 stores (episodic/semantic/procedural/insight, `db.py`), zero-LLM fast capture + LLM "sleep" consolidation, fused surface retrieval (sqlite-vec+BM25+graph) + structural/analogical retrieval (the differentiated feature). arcagent itself is memory-less by default (`NullBrain`); memory is opt-in via `brain = "arcmemory"`.

**Security** — SHIPPED: DID identity (`arctrust/identity.py`), Ed25519 + FIPS-ECDSA signing (`signer.py`), 4-layer policy pipeline (Global/Provider/Team/Sandbox, `policy.py`), 4 audit sinks incl. WORM chain (`audit.py`, `witness.py`), sandbox (VmBackend/Firecracker via arcrun), lethal-trifecta gate (context-resolved 3-leg model per project memory), mechanical HumanGate/operator-approval flow (arcstore.approvals + arc approve CLI + arcui Approvals panel).

**Models/providers (arcllm/arcmodel)** — arcllm SHIPPED: 16 providers, budget/routing config, prompt-caching (cache_control in anthropic.py only), trace store. **arcmodel is an empty placeholder** — its own README says "no public API yet" — routing/model-selection logic actually lives inside arcllm configs today.

**Observability** — SHIPPED: `arcagent/core/telemetry.py`, `arcui/observe.py` + `observe_stats.py`, OTel export gated to federal tier per docs/cli.md tier table.

**Deployment** — SHIPPED (new, this session's branch): single-image Docker (`Dockerfile`, `docker-compose.yml`) — one process serves dashboard + web-chat WS + every enabled platform in-process; HOME=/data pattern for persistent state; multi-arch buildx (amd64/arm64) for DGX Spark + cloud VMs. This supersedes prior systemd-only DGX runbook (reference_dgx_deploy_runbook memory) as an additional install path.

## 3. SPEC LEDGER (selected, from `.claude/specs/ROADMAP-PROGRAM.md`, cross-checked against code)

53 spec folders total. Phase 1 (8 specs: SPEC-033/034/035/036/037/038/039/053) — 🚀 promoted to `main`. Phase 2 (SPEC-040/041/043/044/047) — ✅ merged to `develop`, not yet promoted to main per the roadmap doc's last update. Recent (055-058) predate/postdate the roadmap doc's last edit and show **status-field drift**:

| Spec | Title | README/roadmap status | Code reality |
|---|---|---|---|
| SPEC-055 | Mention-scoped activation | DRAFT | Implemented (`arcteam/mentions.py`) |
| SPEC-056 | Mission Control | DRAFT (spec'd, ready to implement) | Implemented (`arcstore/tasks.py`, `arcagent/modules/tasks/`, arcui tasks routes+page) — matches project memory "SPEC-056 Mission Control DEPLOYED" |
| SPEC-057 | arctrust user identity | PRD draft (pending review) | Unverified — not independently checked beyond PRD existing |
| SPEC-058 | arctui terminal agent | PRD only, no README | arctui package exists but single-screen; can't confirm this spec's scope is fully built |
| SPEC-045 | Scale + interop (incl. real MCP client/server) | PENDING (planning only, per its own README) | Confirmed absent in code (see MCP above) |
| SPEC-042 | Autonomy (proactive engine wiring) | ⬜ pending | `modules/proactive/` and `modules/pulse/` both exist — overlap the roadmap note flags ("retire pulse/scheduler overlap") suggests dual/competing mechanisms, not yet reconciled |
| SPEC-032/046/054/048/049/050/051/052 | Mission control (dup ref)/rogue-agent/replay/ATO/AI-BOM/FIPS-evidence/standards/DX | ⬜ pending | Not implemented — no code found for rogue-agent kill-switch, AI-BOM CI, or ATO package artifacts |

**Producers-unwired pattern**: the roadmap doc itself documents this recurring failure mode across nearly every Phase-1/2 spec review (budget gate unreachable, trifecta dormant until network tools tagged, skill-improver sweep initially unwired, memory no-read-up initially blind) — all were caught by adversarial `/review` and fixed before merge, per the doc's own commit-log entries.

## 4. KNOWN GAPS

- 28 TODO/FIXME/NotImplementedError hits in `packages/*/src`. Clusters:
  - **arcgateway**: `executor_nats.py` + `executor.py` both raise `NotImplementedError` for "multi-instance scaling" (deferred); `pairing_postgres.py` is an unimplemented stub ("Required for federal deployments with >1 gateway" — TODO T1.8.4); `fs_reader.py` raises `NotImplementedError` for any scope other than `"agent"` (no team-shared-knowledge yet); two `# TODO (M1 integration): emit gateway.adapter.fail audit event` gaps in `runner.py`/`_reconnect.py` (adapter-failure audit events not wired).
  - **arcstore**: `backends/__init__.py` — unknown backend name raises `NotImplementedError` (only sqlite backend implemented; deferred Protocol for others, e.g., Postgres for federal multi-gateway).
  - **arcskill**: `hub/trust_backend.py` — TODO(SPEC-033) notes the loader holds a single global trust_backend, not yet per-tenant.
  - **arcteam**: `memory/promotion_gate.py` — TODO: approval-message send via messenger not yet wired.
  - **arcmemory**: `distill.py` has a documented follow-up note (not fully specified in this pass).
- **MCP**: scaffolded config/enum only, no working transport (see §2).
- **arcmodel**: entirely unimplemented (own README says so).
- **arcmas**: not a framework, just a meta pip package — don't count as multi-agent capability.
- **arctui**: thin, single-screen; SPEC-058 scope unclear vs. delivered code.
- **arcagent/core LOC**: 6,328 measured LOC in `core/` vs. the CLAUDE.md 3,500 budget — the roadmap doc's own history shows this ceiling has been breached and re-fixed multiple times (SPEC-037, SPEC-039 core-slim passes); current measurement here is from a fresh recursive `find`, which may include files the LOC-budget script excludes (it globs non-recursively) — treat the 6,328 figure as directionally over-budget, not a precise apples-to-apples breach.

## 5. HEADLINE NUMBERS

- Packages: 18 (2 are stubs/placeholders: arcmas [meta-package], arcmodel [empty scaffold])
- Total non-test Python source LOC: 118,417 (`find packages -name "*.py" -path "*/src/*"`)
- arcui web (TS/TSX) LOC: ~12,863
- Test files: 978 `test_*.py` files; graph reports 8,564 Test nodes
- Graph: 1,672 files / 40,279 nodes / 240,933 edges
