# PRD — SPEC-022: ArcUI Agents + Agent Detail with Live Updates

## 1. Problem & Goal

**Problem.** ArcUI today has only LLM Telemetry + Settings. Operators have no UI surface to see the agents they have, drill into one, watch it work, or inspect its identity, policy, skills, memory, sessions, tools, traces, and files. Today they `cd` into `team/<agent>/` in a terminal — that's not a federal-grade observability story.

**Goal.** Give operators a complete read-only observability and control surface for the agent fleet — built as a clean two-layer system where `arcgateway` owns all data access and `arcui` is a pure presentation consumer.

## 2. Users & Roles

| Role | Capability |
|------|-----------|
| **Viewer** | All read-only screens, all fs reads via gateway, audit-trace visible |
| **Operator** | Viewer + Pause/Restart via existing `/control` endpoint |
| **Admin** | (future) Deploy, configure — out of scope this spec |

Role enforcement rides on existing arctrust PolicyPipeline.

## 3. User Stories

### US-1 — Fleet visibility
As a Viewer, I open ArcUI, click **Agent Fleet** in the sidebar, and see two stat boxes — **Total** (every agent declared in `team/`) and **Live** (currently connected) — plus an idle count and avg memory. I see a card for each agent with name, DID, status, model, and 24h activity numbers. I filter by org or status.

### US-2 — Agent deep-dive
I click an agent card and land on **Agent Detail** at `?agent=<id>`. The header shows avatar, DID, status, last seen, org, type, workspace path. Pause and Restart buttons work. Deploy is rendered but disabled with a "Coming soon" tooltip.

### US-3 — Inspect identity
On the **Identity** tab I see the DID document (Prism-highlighted JSON), Ed25519 public key, namespace permissions, key rotation history, recent signatures, and a trust chain visual. Private keys never appear — the field shows "VAULT-SECURED."

### US-4 — Watch policy evolve (ACE)
On the **Policy** tab I see ACE stats (active bullets, avg score, reviewed 24h, retired). The lifecycle diagram (Generator → Reflector → Curator → Feedback). System policy rules table. **Self-learned bullets** parsed from `policy.md`: each bullet has a P-id, agent badge, score bar (green ≥7 / yellow 4–6 / red ≤3), full text, and meta footer (uses · reviewed · created · source-session). I sort and filter; I click `source:` and jump to that session.

### US-5 — See policy update live
The agent's curator rewrites `policy.md`. Within 2 seconds my open browser re-renders the bullets — no refresh, no polling spike. arcgateway watcher detected the change, re-parsed, and pushed a `policy:bullets_updated` event through stream_bridge.

### US-6 — Browse memory and files
On **Memory** I see a collapsible file tree of `workspace/`. Click a folder, it expands; click a `.md`, it renders (h2/lists/code fences); click a `.py`, it Prism-highlights. State persists in localStorage. On **Files** I see the same pattern but rooted at the agent root — `arcagent.toml`, `tools/`, `capabilities/`, `traces/` — readable without leaving the browser.

### US-7 — Replay a session
On **Sessions** I see all session JSONL files with metadata. I click one and the conversation replays — user / assistant / tool turns, markdown content rendered, tool calls highlighted. New messages append live while I watch.

### US-8 — Audit a control action
On **Security & Audit** I see a live tail of every audit event: file reads, control commands, policy decisions, connection handshakes. I filter to `agent_id=josh-agent`, see who paused it and when, the response status, and the trace id.

### US-9 — Cross-agent fleet views
**Tasks**, **Tools & Skills**, and **Policy Engine** sidebar pages aggregate across the fleet — task lists with click-to-detail, tool/agent matrix, all bullets across all agents.

### US-10 — Agent self-describes display hints
I add `[ui] color = "#ff6b6b"` to my agent's `arcagent.toml`. Within 2 seconds my fleet view re-renders that agent's avatar in the new color. No restart, no UI redeploy.

## 4. Functional Requirements

### F1 — Agents fleet screen
- F1.1 Stat row: Total / Live / Idle / Avg Memory
- F1.2 Filter pills: All / Online / Offline / by org
- F1.3 Agent grid with avatar, name, DID, status, role, model, 24h sessions/tasks/tokens/cost, sparkline
- F1.4 Click → `?page=agent-detail&agent=<id>`
- F1.5 Tool Access Matrix table
- F1.6 Module Status table
- F1.7 Header: `+ Deploy Agent` (disabled), `Filter`

### F2 — Agent Detail header
- F2.1 Avatar + name + DID + status + role + last seen + org + type + workspace path
- F2.2 Pause / Restart buttons → existing `POST /api/agents/{id}/control`
- F2.3 Deploy button rendered, disabled, tooltip "Coming soon"

### F3 — Overview tab
- F3.1 Cryptographic Identity card (DID, org, type, key algo, public key, key file perms, created, last signature)
- F3.2 Configuration card (parsed `arcagent.toml`: model, max_tokens, temperature, context window, prune/compact/emergency thresholds, tool timeout, vault backend, retention, eval config)
- F3.3 Registered Tools table (transport, 24h calls, avg duration)
- F3.4 Context Window card (% used, threshold pills)
- F3.5 Performance gauges (uptime, avg response, tool success)
- F3.6 Recent Sessions table
- F3.7 Active Modules table
- F3.8 Pulse panel (if `pulse.md` / `pulse-state.json` exists)
- F3.9 Tasks sub-card if `workspace/tasks.json` exists
- F3.10 Schedules sub-card if `workspace/schedules.json` exists
- F3.11 Raw TOML expand button on Configuration

### F4 — Identity tab
- F4.1 DID Document (Prism JSON)
- F4.2 Ed25519 Keypair card (public-only; private = "VAULT-SECURED" badge)
- F4.3 Namespace Permissions table
- F4.4 Key Rotation History
- F4.5 Recent Signatures
- F4.6 Trust Chain visual (root CA → operator → agent)

### F5 — Sessions tab
- F5.1 Stat cards (total, messages, avg length, compactions)
- F5.2 Active Session card (id, started, duration, messages, tokens, tool calls, llm calls, cost, checkpoint)
- F5.3 Context Window Timeline
- F5.4 Session History table (clickable)
- F5.5 Session viewer — JSONL replayed as conversation, markdown rendered, tool calls highlighted, paginated 50/page
- F5.6 Compaction Events timeline

### F6 — Skills tab
- F6.1 Loaded Skills cards from `workspace/skills/*.md` with frontmatter parsed
- F6.2 Click → renders SKILL.md
- F6.3 Skill Evaluation Log table
- F6.4 Skill Usage 7-day bars
- F6.5 Module Signatures table

### F7 — Memory tab (file tree)
- F7.1 Collapsible tree rooted at `workspace/`. Folders: memory/, notes/, entities/, library/, files/, sessions/, archive/, slack/, skills/
- F7.2 File-count badges per folder
- F7.3 Expand/collapse state in localStorage
- F7.4 Right-pane viewer — markdown rendered, code Prism-highlighted, JSON pretty-printed
- F7.5 Header: path / size / mtime / "Copy path"
- F7.6 Memory Statistics card pinned at bottom

### F8 — Policy tab (ACE)
- F8.1 Stat cards: Active Bullets / Avg Score / Reviewed 24h / Retired
- F8.2 ACE Lifecycle diagram (Generator → Reflector → Curator → Feedback Loop)
- F8.3 System Policy Rules table (operator-defined)
- F8.4 Self-Learned Bullets card — parsed via regex
- F8.5 Bullet element: id chip, agent badge, score bar (green ≥7, yellow 4–6, red ≤3), score, text, meta footer (uses · reviewed · created · source link)
- F8.6 Sort: score / uses / recently reviewed / recently created
- F8.7 Filter pills: All / High / Mid / Low / Retired
- F8.8 Click bullet → expand; click `source:` → jump to that session in Sessions tab
- F8.9 Score Distribution bars
- F8.10 Top Performing Bullets card
- F8.11 Reflection Activity 7-day chart
- F8.12 Rendered policy.md collapsible with "View raw" toggle

### F9 — Tools tab
- F9.1 Per-tool list (transport, allow/sandbox/deny, timeout, 24h calls, avg duration, success rate, last called)
- F9.2 Source viewer (Prism on `capabilities/<tool>.py` or `tools/<tool>.py`; for MCP/HTTP shows endpoint + auth method)
- F9.3 Tool Allowlist/Denylist from `[tools.policy]`

### F10 — Telemetry tab
- F10.1 Stat cards (LLM calls, tokens, avg latency, cost — 24h)
- F10.2 Token Usage chart (input vs output)
- F10.3 Recent LLM Calls table with trace links
- F10.4 Cost Breakdown
- F10.5 Recent Trace Spans waterfall (agent.turn / llm.request / tool.* / policy.check / memory.consolidate)
- F10.6 Latency Distribution (p50/p90/p95/p99/max)

### F11 — Files tab
- F11.1 Same file-tree pattern as Memory, rooted at agent root (`team/<agent>/`)
- F11.2 Markdown rendered, code Prism-highlighted, JSON pretty-printed
- F11.3 Read-only viewer

### F12 — Cross-tab elements
- F12.1 Live Event Drawer — slide-out tail of UIEvents filtered to current agent
- F12.2 Audit Log Viewer — same pattern, audit events

### F13 — Tasks fleet page
- F13.1 Read each agent's `workspace/tasks.json` via gateway
- F13.2 Render fleet task list: agent · task · status · priority · created · due
- F13.3 Click task → opens that agent's Detail view

### F14 — Tools & Skills fleet page
- F14.1 Tools matrix (rows = unique tools, cols = each agent)
- F14.2 Skills directory (skill name → agents using → invocations 24h → avg success rate)

### F15 — Security & Audit fleet page
- F15.1 Live audit event stream (filterable by agent / event type)
- F15.2 Recent control actions log
- F15.3 Failed-policy events
- F15.4 Connection security panel (mTLS status, agent auth method, last handshake)

### F16 — Policy Engine fleet page
- F16.1 Aggregated stats across all agents
- F16.2 All bullets across all agents (each with agent badge)
- F16.3 Per-agent breakdown table (bullet counts, avg scores)
- F16.4 Mutation history timeline derived from comparing reviewed dates

### F17 — `[ui]` config section
- F17.1 Optional block in `arcagent.toml`. Fields: `display_name`, `color`, `role_label`, `hidden`. All optional with defaults.
- F17.2 Read by `arcgateway.config.load_ui_section()`. Surfaced through roster endpoint.
- F17.3 Watcher on `arcagent.toml` re-emits roster update on `[ui]` change.

### F18 — Live update infrastructure
- F18.1 `arcgateway.fs_watcher` lazy-starts per agent on first subscriber, ref-counted
- F18.2 Emits `FileChangeEvent` through existing `stream_bridge`
- F18.3 Polling fallback (mtime, 2s) when watchfiles unavailable
- F18.4 arcui `/ws` extended with `subscribe:agent:<id>` / `unsubscribe:agent:<id>`
- F18.5 Reconnect replays last N events from `event_buffer`

## 5. Non-Functional Requirements

| ID | Requirement | Pillar |
|----|-------------|--------|
| NF-1 | Cold start adds < 100ms | Scalability |
| NF-2 | File change → browser update within 2s | Scalability |
| NF-3 | All gateway fs ops audited (NIST AU-2) | Security |
| NF-4 | Read-only by structure — no write methods on fs_reader | Security |
| NF-5 | Path traversal blocked at single chokepoint | Security |
| NF-6 | 1MB file read cap; depth-10 tree cap; 50-msg session pagination | Security, Scalability |
| NF-7 | Watchers ref-counted; max-watchers configurable cap | Scalability |
| NF-8 | Vendored Prism + zero CDN deps (federal/air-gap) | Security |
| NF-9 | `mypy --strict`, `ruff check` clean | Simplicity |
| NF-10 | Coverage ≥ 80% line / 75% branch / 90% on new modules | Simplicity |

## 6. Acceptance Criteria

(Mirrors README §Acceptance Criteria — 21 items.)

## 7. Out of Scope

- Deploy functionality
- Send-message to agent
- Dashboard / Team Comms / ArcRun Monitor / Knowledge Base sidebar items
- Write surfaces from UI to identity/policy/config
- Team-shared scope (`team/shared/`) — forward-compat arg only

## 8. Dependencies

- Adds `watchfiles>=0.21` to `arcgateway/pyproject.toml` only
- Vendor Prism.js locally in arcui static
- Reuses: SPEC-015 RollingAggregator, SPEC-016 AgentRegistry/UIEvent/control-proxy, SPEC-019 Entity.workspace_path + browser bootstrap auth

## 9. Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| Watcher fork explosion across many agents | Ref-counted; lazy; max-watcher cap; idle timeout |
| Large `policy.md` slow to parse | Cap parser at 10k lines; cache parsed result keyed by mtime |
| Browser flooded with file events on bulk edits | Debounce 250ms per (agent_id, path) at gateway emit point |
| Path traversal regression | Single chokepoint + dedicated test + grep guard test |
| arcui drift back to direct fs access | Static grep guard test (acceptance criterion 16) fails CI if any arcui code touches team/ paths |
