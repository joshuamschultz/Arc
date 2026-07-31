# 2026 Agent Platform UI/UX Teardown

Research date: 2026-07-25. Sources are 2026 blog posts, changelogs, docs, DeepWiki pages, Reddit/HN threads. Every claim is sourced; anything not found in a source is marked **[UNVERIFIED]**.

---

## (a) Screen Inventory Matrix

| Screen/Panel | Goose | Claude Code | OpenCode | Grok Build | Codex | Antigravity/Gemini | Cursor | Amp | Devin | OpenHands | Cline | Warp | Factory | Replit Agent | v0 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Session/chat surface | Yes (Desktop+CLI) | Yes (CLI/desktop/web/IDE) | Yes (TUI) | Yes (CLI+web) | Yes (CLI/IDE/web) | Yes | Yes | Yes | Yes (web) | Yes (web "Agent Canvas") | Yes (sidebar) | Yes (in-terminal) | Yes | Yes | Yes (chat+preview) |
| Session list/history | `SessionListView` grid of cards [DeepWiki](https://deepwiki.com/block/goose/3.1.4-session-and-recipe-management-ui) | multi-session sidebar [BuildFastWithAI](https://www.buildfastwithai.com/blogs/claude-code-desktop-redesign-2026) | session state persists (LocalProvider) [DeepWiki](https://deepwiki.com/anomalyco/opencode/3.1-terminal-user-interface-(tui)) | web UI for monitoring [BuildFastWithAI](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026) | thread menus, nav buttons in header [OpenAI Codex changelog](https://developers.openai.com/codex/changelog?type=codex-cli) | agent manager [ofox.ai](https://ofox.ai/blog/google-antigravity-2-explained-gemini-desktop-agent-platform-2026/) | agents/plans/runs as sidebar objects [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/) | thread-based | session-based, Slack/GitHub triggered [ChatPRD](https://www.chatprd.ai/how-i-ai/how-i-ai-devin-ai) | web planning/monitoring UI [aiagentslist](https://aiagentslist.com/agents/openhands) | sidebar in VS Code [DeployHQ](https://www.deployhq.com/guides/cline) | vertical tabs across agents [DigitalApplied](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide) | sidebar sessions w/ own context [Factory.ai](https://factory.ai/news/factory-desktop) | workspace-based | chat history |
| File tree / editor | — | in-app file editor (2026 redesign) [BuildFastWithAI](https://www.buildfastwithai.com/blogs/claude-code-desktop-redesign-2026) | — | — | Finder/Explorer jump-to-folder [OpenAI changelog](https://developers.openai.com/codex/changelog?type=codex-cli) | codebase-aware IDE | full IDE (VS Code fork) | VS Code extension | — | — | VS Code sidebar | — | — | code workspace | code preview |
| Diff viewer | — | inline via terminal/desktop | — | — | — | Artifacts panel | unified multi-file diff [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/) | — | PR-style diffs | code review in web UI | Plan Mode preview before Act [DeployHQ](https://www.deployhq.com/guides/cline) | code review pane [DigitalApplied](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide) | — | — | Git panel w/ diff [NxCode](https://www.nxcode.io/resources/news/v0-by-vercel-complete-guide-2026) |
| Integrated terminal | native (CLI is the app) | built-in terminal pane (2026 redesign) | native | native | in-app browser (not terminal) | CLI surface | integrated | terminal UI + VS Code | cloud sandbox | sandboxed Docker | preview CLI (macOS/Linux) | IS the terminal | Terminal UI product | shell/runtime container | sandbox runtime |
| Task board / task list | recipes list | — | — | — | — | — | agents/plans list | — | task queue (Slack/issue driven) | — | — | agent status list | multi-session sidebar | — | — |
| Traces/observability | — | — | — | — | — | — | — | — | DeepWiki repo docs (not traces) | — | — | — | — | — |
| Cost/token dashboard | — | shows cost/tokens (chat) | — | — | — | — | — | 200K context awareness | enterprise cost visibility | cost visibility dashboard (Enterprise) [aiagentslist](https://aiagentslist.com/agents/openhands) | "Spend Limit Reached" UI [DevOps guide](https://www.deployhq.com/guides/cline) | — | — | — | — |
| Agent roster / multi-agent manager | — | — | — | up to 8 parallel sub-agents [BuildFastWithAI](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026) | — | agent manager, multi-agent orchestration default [thenextweb](https://thenextweb.com/news/google-antigravity-2-desktop-cli-sdk-io-2026) | parallel agents, hop between like terminals [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/) | sub-agents report to main thread [Skywork](https://skywork.ai/skypage/en/ultimate-guide-amp-ai-coding/2044314087912464384) | — | TaskToolSet sub-agent delegation [aiagentslist](https://aiagentslist.com/agents/openhands) | — | management UI showing all running agents [DigitalApplied](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide) | multi-session desktop, mobile view [Factory.ai](https://factory.ai/news/factory-desktop) | — | — |
| Approvals/permissions queue | Extensions consent | `/permissions` UI, Manual/auto modes [Docs](https://code.claude.com/docs/en/permissions) | — | — | — | — | guardrails on Agent Mode | — | — | role mgmt (owner/admin/member) | Plan→Act gate | — | — | — | — |
| Marketplace / extensions | 70+ MCP extensions, "Advanced settings → Extensions" [knightli.com](https://knightli.com/en/2026/05/08/goose-open-source-ai-agent-desktop-cli-api/) | plugin marketplaces (official/3rd-party/private) [buildtolaunch](https://buildtolaunch.substack.com/p/mcp-server-types-installation-guide-claude-cursor) | — | — | — | Antigravity "plugins" (renamed from extensions) [aidailypost](https://aidailypost.com/news/google-antigravity-20-retains-gemini-cli-features-antigravity-plugins) | Settings → MCP → Add server | — | — | internal add-on marketplace (Enterprise) [aiagentslist](https://aiagentslist.com/agents/openhands) | — | first-class MCP support [DigitalApplied](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide) | plugin support in Terminal UI [Factory.ai](https://factory.ai/product/cli) | — | — |
| Memory/context browser | — | CLAUDE.md/context files | — | — | AGENTS.md | — | — | — | — | — | — | — | — | workspace instructions/skills [Replit changelog](https://docs.replit.com/updates/2026/06/12/changelog) | — |
| Settings/theming | — | theming | themes/keybinds customizable [DeepWiki](https://deepwiki.com/anomalyco/opencode/3.1-terminal-user-interface-(tui)) | — | full theme editor: base theme, accent/bg/fg colors, fonts, shareable themes [OpenAI changelog](https://developers.openai.com/codex/changelog?type=codex-cli) | — | — | — | — | — | — | — | — | — | — |

Note: blanks are not proof of absence — they mean no source surfaced that screen for that product; do not read a blank as "doesn't exist."

---

## (b) Affordance / Button Checklist — Who Has What

| Affordance | Confirmed on | Source |
|---|---|---|
| Approve/deny tool call | Claude Code (`/permissions`, allow/ask/deny rules, evaluated deny→ask→allow) | [Docs](https://code.claude.com/docs/en/permissions) |
| "Manual" vs "Auto" permission mode (renamed from "default") | Claude Code, CLI/VS Code/JetBrains/desktop, July 2026 | [Docs](https://code.claude.com/docs/en/permissions) |
| Plan-then-Act gate (propose before executing) | Cline (Plan Mode → Act Mode) | [DeployHQ](https://www.deployhq.com/guides/cline) |
| Rewind/checkpoint (revert files+chat to a prior turn) | Claude Code — every prompt auto-checkpoints; Esc×2 or `/rewind`; options "Restore code and conversation" or "Summarize up to here" | [Docs](https://code.claude.com/docs/en/checkpointing), [explainx](https://explainx.ai/blog/claude-code-restore-checkpoint-feature-2026) |
| Fork session (branch without altering original) | Claude Code (`claude --continue --fork-session`) | [wmedia](https://wmedia.es/en/tips/rewind-changes-instantly-with-checkpoints) |
| Checkpoint 30-day auto-cleanup | Claude Code | [Docs](https://code.claude.com/docs/en/checkpointing) |
| Interrupt / steer mid-run | Claude Code web ("actively steer Claude to adjust course") | [Anthropic](https://www.anthropic.com/news/claude-code-on-the-web) |
| Teleport (pull a web session + its branch to local terminal) | Claude Code web | [Anthropic](https://www.anthropic.com/news/claude-code-on-the-web) |
| Live shareable Artifact from a session | Claude Code (beta, Team/Enterprise, mid-June 2026) | [Heurekadevs](https://www.heurekadevs.com/claude-code-for-the-web-your-safest-first-step-into-ai-assisted-development) |
| Unified multi-file diff review | Cursor | [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/) |
| Bugbot auto-fix suggestions on PRs (learns from feedback) | Cursor (~80% resolve rate claimed) | [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/) |
| Design Mode (visual/frontend editing) | Cursor | [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/) |
| Multi-LLM comparison in one session | Cursor | [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/) |
| Parallel sub-agents from one prompt (up to 8) | Grok Build | [BuildFastWithAI](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026) |
| Open file location in Finder/Explorer from a thread | OpenAI Codex app | [OpenAI changelog](https://developers.openai.com/codex/changelog?type=codex-cli) |
| In-app browser to click through rendered UI / repro visual bugs | OpenAI Codex app; Claude Code (Cmd+Shift+B) | [OpenAI changelog](https://developers.openai.com/codex/changelog?type=codex-cli), [explainx](https://www.explainx.ai/blog/claude-code-desktop-browser-built-in-july-2026) |
| Full custom theme editor with shareable themes | OpenAI Codex app | [OpenAI changelog](https://developers.openai.com/codex/changelog?type=codex-cli) |
| Drag-and-drop pane layout, split panels | Claude Code desktop redesign | [BuildFastWithAI](https://www.buildfastwithai.com/blogs/claude-code-desktop-redesign-2026) |
| Recipes (saved, shareable repeatable workflows) | Goose | [aitoolanalysis](https://aitoolanalysis.com/goose-ai-review/) |
| Interactive in-session UI from extensions (forms/charts/wizards, "MCP Apps") | Goose | [LLMReference](https://www.llmreference.com/agents/goose) |
| Spend-limit hard stop UI | Cline (v3.78, "Spend Limit Reached") | [DeployHQ](https://www.deployhq.com/guides/cline) |
| Command blocks + searchable history + sharable workflows | Warp | [DigitalApplied](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide) |
| Cross-tool agent management UI (Claude Code/Codex/Gemini CLI/OpenCode side by side) | Warp | [DigitalApplied](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide) |
| Mobile remote access to running agents | Warp, Factory | [DigitalApplied](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide), [Factory.ai](https://factory.ai/news/factory-desktop) |
| Persistent per-agent machine (packages/repos/creds survive) | Factory | [Factory.ai](https://factory.ai/news/factory-desktop) |
| Agent decides its own visualization (Mermaid, charts, dashboards in-conversation) | Factory | [Factory.ai](https://factory.ai/news/factory-desktop) |
| Screenshot/Figma → pixel-accurate rebuild in one pass | Replit Agent | [Aakash Gupta](https://www.news.aakashg.com/p/guide-replit) |
| Multi-select / hover-state / responsive-override visual editing | Replit Agent (Design Canvas) | [Aakash Gupta](https://www.news.aakashg.com/p/guide-replit) |
| In-chat Git panel: branch, commit, open PR without leaving the tool | v0 | [NxCode](https://www.nxcode.io/resources/news/v0-by-vercel-complete-guide-2026) |
| Sandbox runtime mirroring prod, import existing codebases | v0 | [NxCode](https://www.nxcode.io/resources/news/v0-by-vercel-complete-guide-2026) |
| Devin Outposts — run cloud session against your own hardware (workstation, VM, K8s, Mac) | Devin | [Cognition](https://www.buildfastwithai.com/ai-tools/devin) |
| DeepWiki auto-generated architecture diagrams + chat over any repo | Devin/Cognition | [Oflight](https://www.oflight.co.jp/en/columns/deepwiki-cognition-repo-documentation-2026-07) |
| Agent Canvas (replaced legacy CLI+local GUI as default surface) | OpenHands | [aiagentslist](https://aiagentslist.com/agents/openhands) |
| Enterprise cost-visibility dashboard, role mgmt, internal marketplace | OpenHands Enterprise | [aiagentslist](https://aiagentslist.com/agents/openhands) |
| Agent graph visualization of tool/retrieval flow | Langfuse | [Langfuse docs](https://langfuse.com/docs/observability/overview) |
| Prebuilt + fully custom multi-metric dashboards | Langfuse, LangSmith | [Langfuse](https://langfuse.com/docs/metrics/features/custom-dashboards), [LangChain docs](https://docs.langchain.com/langsmith/dashboards) |
| Click a trace-tree node → detail panel with inputs/outputs/metrics | LangSmith | [Stackademic](https://blog.stackademic.com/langsmith-monitor-debug-test-and-evaluate-2e3d51681d6b) |
| AI-suggested fixes for failing/expensive traces | LangSmith Engine (new 2026) | [AI.cc](https://www.ai.cc/blogs/how-to-use-langsmith-2026-complete-guide/) |
| Pause/resume live event stream; filter by event type/failed-only | Temporal UI | [Temporal docs](https://docs.temporal.io/web-ui) |
| Click event → see Scheduled/Started/Completed lineage | Temporal UI | [Temporal changelog](https://temporal.io/changelog/updated-event-history-timeline-view-is-now-available) |

---

## (c) Eight Workflows — Step by Step for 3 Best Products

Chosen: **Claude Code** (richest surface variety), **Goose** (primary benchmark, open-source reference), **Cursor** (best-documented IDE-agent flow).

### 1. First-run onboarding & connecting a model/key
- **Claude Code**: install CLI/desktop → OAuth login to Anthropic account (Pro/Max/Team/Enterprise) → desktop redesign auto-detects repos and opens multi-session sidebar. [BuildFastWithAI](https://www.buildfastwithai.com/blogs/claude-code-desktop-redesign-2026)
- **Goose**: install CLI or Desktop (Electron, macOS/Linux/Windows/deb/rpm/Flatpak) → choose from 15+ providers (incl. local Ollama) → configure extensions via "Advanced settings → Extensions → Add custom extension" (STDIO command). [knightli.com](https://knightli.com/en/2026/05/08/goose-open-source-ai-agent-desktop-cli-api/), [buildtolaunch](https://buildtolaunch.substack.com/p/mcp-server-types-installation-guide-claude-cursor)
- **Cursor**: install IDE (VS Code fork) → sign in → Agent Mode/Composer available immediately; JetBrains users connect via ACP as of March 4, 2026. [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/)

### 2. Starting a task
- **Claude Code**: type a prompt in CLI/desktop/web; web sessions run in an isolated environment with real-time progress tracking. [Anthropic](https://www.anthropic.com/news/claude-code-on-the-web)
- **Goose**: start a session or invoke a saved **recipe** (a reusable, shareable workflow) — recipes are the feature Reddit users cite most as differentiating. [aitoolanalysis](https://aitoolanalysis.com/goose-ai-review/)
- **Cursor**: open the dedicated agent layout, prompt Agent Mode, optionally spin up parallel agents for different sub-tasks (refactor / fix-tests / UI-polish) that you hop between like terminal tabs. [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/)

### 3. Reviewing and accepting changes
- **Claude Code**: permission system evaluates deny→ask→allow per tool call; "Manual" mode approves every write/bash call by hand, "Auto" mode (shipped March 24, 2026) reduces prompts while keeping guardrails — Anthropic reports 93% of prompts get approved anyway. [Docs](https://code.claude.com/docs/en/permissions)
- **Goose**: extension/tool calls surface inline; MCP Apps let an extension render an interactive form/wizard for approval-like confirmation instead of plain text. [LLMReference](https://www.llmreference.com/agents/goose)
- **Cursor**: unified diff view across all touched files (frontend/backend/config) in one pass, described by users as "a mini PR review"; Bugbot pre-screens and auto-fixes ~80% of issues it raises before a human even looks. [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/)

### 4. Recovering from a bad run
- **Claude Code**: press Esc twice or `/rewind` → menu of every prior prompt as a checkpoint → "Restore code and conversation" (full revert) or "Summarize up to here" (compress without reverting); checkpoints persist 30 days. [Docs](https://code.claude.com/docs/en/checkpointing)
- **Goose**: **[UNVERIFIED]** — no sourced checkpoint/rewind equivalent found for Goose; session history exists (SessionHistoryView) but revert semantics weren't documented in sources reviewed.
- **Cursor**: Composer retains diff-review-before-apply as the primary safety net; no sourced rewind/checkpoint feature found — **[UNVERIFIED]** beyond diff review + git.

### 5. Running something in the background and coming back to it
- **Claude Code**: Routines subscribe a session to GitHub repo events (PR opened, push, issues, releases, checks, discussions) via webhook, spinning a new session per event; desktop redesign improved "background agent handling." [gradually.ai](https://www.gradually.ai/en/changelogs/claude-code/), [BuildFastWithAI](https://www.buildfastwithai.com/blogs/claude-code-desktop-redesign-2026)
- **Goose**: **[UNVERIFIED]** for native background/scheduling; not found in sources.
- **Cursor**: parallel agents run concurrently in the sidebar; user switches between them like switching terminals/branches — closest analog to "leave and come back." [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/)
- (Best-in-class comparison point outside the three: **Warp** ships a management UI showing status of *all* running agents plus notifications, and **Factory** gives every Droid a persistent machine plus mobile check-in. [DigitalApplied](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide), [Factory.ai](https://factory.ai/news/factory-desktop))

### 6. Sharing a result with a teammate
- **Claude Code**: turn a session's output into a live, shareable Artifact page on claude.ai that keeps updating as the session works (beta, Team/Enterprise, mid-June 2026). [Heurekadevs](https://www.heurekadevs.com/claude-code-for-the-web-your-safest-first-step-into-ai-assisted-development)
- **Goose**: **[UNVERIFIED]** — no sourced share/permalink feature.
- **Cursor**: **[UNVERIFIED]** for a dedicated share/permalink; sourced material covers review/diff, not sharing.

### 7. Adding a tool/extension/MCP server
- **Claude Code**: `claude mcp add <name> --command="npx" --args="..."`; plugins can publish to Anthropic's official marketplace, third-party marketplaces, or a private team marketplace. [buildtolaunch](https://buildtolaunch.substack.com/p/mcp-server-types-installation-guide-claude-cursor)
- **Goose**: Advanced settings → Extensions → Add custom extension → STDIO type → set command → Add Extension; 70+ extensions available out of the box. [buildtolaunch](https://buildtolaunch.substack.com/p/mcp-server-types-installation-guide-claude-cursor), [LLMReference](https://www.llmreference.com/agents/goose)
- **Cursor**: Cursor Settings → MCP → Add new MCP Server → name it, set command type + command. [buildtolaunch](https://buildtolaunch.substack.com/p/mcp-server-types-installation-guide-claude-cursor)
- All three share the same underlying JSON MCP config shape, so server packages are largely interchangeable. [buildtolaunch](https://buildtolaunch.substack.com/p/mcp-server-types-installation-guide-claude-cursor)

### 8. Creating a new agent
- **Claude Code**: **[UNVERIFIED]** — sources cover sessions/routines, not a distinct "create a new named agent" flow beyond sub-agents/CLAUDE.md config.
- **Goose**: recipes function as reusable agent configurations (provider + extensions + prompt) that can be shared. [aitoolanalysis](https://aitoolanalysis.com/goose-ai-review/)
- **Cursor**: spin up a new parallel agent instance from the agent sidebar for a distinct task; not a persistent "named agent" creation flow in sourced material. [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/)
- (Best sourced example of real "agent creation" as a first-class flow: **Antigravity 2.0**'s SDK for building custom agents, and **OpenHands**' Python SDK/REST API for custom agent orchestration. [thenextweb](https://thenextweb.com/news/google-antigravity-2-desktop-cli-sdk-io-2026), [aiagentslist](https://aiagentslist.com/agents/openhands))

---

## (d) Delight List (genuinely copyable)

- **Claude Code — checkpoint/rewind with dual restore modes** ("restore code+conversation" vs "summarize up to here" lets you compress context without losing work). [Docs](https://code.claude.com/docs/en/checkpointing)
- **Claude Code — Teleport**: seamlessly hand a cloud/web session (with its branch) down to your local terminal. [Anthropic](https://www.anthropic.com/news/claude-code-on-the-web)
- **Claude Code — Routines**: subscribe a session to any GitHub webhook event; zero-touch agent spin-up on `pull_request.opened` etc. [gradually.ai](https://www.gradually.ai/en/changelogs/claude-code/)
- **Goose — Recipes**: the single most-praised feature in community sentiment; turns a one-off session into a shareable, repeatable workflow artifact. [aitoolanalysis](https://aitoolanalysis.com/goose-ai-review/)
- **Goose — MCP Apps**: extensions render real interactive UI (forms, charts, wizards) inside the session instead of walls of text. [LLMReference](https://www.llmreference.com/agents/goose)
- **Cursor — Bugbot**: pre-screens/auto-fixes ~80% of the issues it raises and learns from PR feedback over time, cutting human review load. [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/)
- **Cursor — agent-as-first-class-object sidebar**: agents/plans/runs are objects you hop between like terminal tabs, not a chat buried in a panel. [Sean Kim](https://blog.imseankim.com/cursor-3-agents-window-review-parallel-agents-design-mode-claude-code-codex-april-2026/)
- **Warp — cross-tool agent management**: one pane shows Claude Code, Codex, Gemini CLI, and OpenCode side by side with unified status/notifications — the "one ring to rule them all" affordance nobody else has. [DigitalApplied](https://www.digitalapplied.com/blog/warp-ai-terminal-agentic-cli-workflows-guide)
- **Factory — persistent machine per Droid + mobile handoff**: start on laptop, check progress from phone, nothing gets torn down between turns. [Factory.ai](https://factory.ai/news/factory-desktop)
- **Factory — agent decides its own visualization** (Mermaid/chart/dashboard chosen by the agent, not hardcoded). [Factory.ai](https://factory.ai/news/factory-desktop)
- **Replit — screenshot/Figma → high-fidelity one-pass rebuild** with precise post-hoc controls (multi-select, hover-state editing, responsive overrides). [Aakash Gupta](https://www.news.aakashg.com/p/guide-replit)
- **v0 — in-chat Git panel**: branch/commit/PR without ever leaving the chat surface. [NxCode](https://www.nxcode.io/resources/news/v0-by-vercel-complete-guide-2026)
- **Devin — Outposts**: same cloud session, but executing on your own hardware (workstation/VM/K8s/Mac) — bridges "fully managed" and "run where my data lives." [Cognition](https://www.buildfastwithai.com/ai-tools/devin)
- **DeepWiki**: swap `github.com`→`deepwiki.com` in any URL and get an auto-generated architecture-diagram wiki + chat, 50k+ repos pre-indexed. [Oflight](https://www.oflight.co.jp/en/columns/deepwiki-cognition-repo-documentation-2026-07)
- **LangSmith — click-through trace tree**: click any node in the hierarchical run tree, detail panel opens with exact inputs/outputs/metrics on the right — the trace-navigation UX every agent-control-plane should copy. [Stackademic](https://blog.stackademic.com/langsmith-monitor-debug-test-and-evaluate-2e3d51681d6b)
- **Temporal UI — pause live event stream + filter failed-only**: lets you freeze a fast-moving timeline mid-flight to investigate, then resume. [Temporal docs](https://docs.temporal.io/web-ui)
- **OpenAI Codex app — shareable custom themes**: full base/accent/bg/fg/font theming that can be exported and shared, not just light/dark toggle. [OpenAI changelog](https://developers.openai.com/codex/changelog?type=codex-cli)

---

## (e) Complaint List (real user pain)

- **Claude Code permission fatigue**: default "Manual" mode requires approval for every file write/bash call; on long refactors users click "approve" repeatedly ("like a zombie"), driving some to `--dangerously-skip-permissions` despite the risk of exposing SSH keys/API tokens/prod DB creds on that machine. Anthropic's own docs acknowledge "prompt fatigue mitigation" as a deliberate tradeoff; internal data says 93% of prompts get approved anyway, which itself is cited as evidence the prompts are often theater rather than real friction-reducing signal. [MindStudio-adjacent/quantitative](https://thequantitative.medium.com/why-claude-code-keeps-asking-for-permission-and-how-to-make-it-stop-0c4d687c0031), [Docs](https://code.claude.com/docs/en/permissions)
- **Goose — security exposure ("Operation Pale Fire")**: Block's own security team hacked Goose in Jan 2026 via a combined phishing + prompt-injection exercise, publicly disclosed — a concrete instance of the "Lethal Trifecta" risk (private data + external comms + untrusted input) materializing against a real product. [Fortune-adjacent/theaiagentindex](https://theaiagentindex.com/agents/goose)
- **Goose — assumes terminal fluency**: reviewers flag that Goose assumes comfort with terminals, config files, and dev tooling, i.e. it is not approachable for less technical users. [theaiagentindex](https://theaiagentindex.com/agents/goose)
- **Cursor — IDE lock-in**: committing to Cursor's VS Code fork rather than augmenting an existing editor creates real friction for JetBrains/Vim/Neovim/Emacs users (keybinding conflicts, workflow disruption). [checkthat.ai-adjacent](https://checkthat.ai/brands/cursor/reviews)
- **Background-agent notification gap is a market-wide sore spot**: multiple third-party products exist *purely* to patch this (Pushary, ai-agent-notifier, AgentsRoom, ntfy/Novu routing) — strong signal that native "your agent is done" notification UX is broadly underbuilt outside Warp/Claude Code/Factory. Also a live bug report: background-process completion notifications getting treated as user messages and polluting conversation history in at least one agent product. [GitHub feature reqs](https://github.com/hesreallyhim/awesome-claude-code/issues/2105), [GitHub bug](https://github.com/NousResearch/hermes-agent/issues/35298)
- **v0 — no component memory across projects**: design-system decisions aren't remembered between sessions, so recurring choices (spacing, tokens) get re-litigated by the AI each time. [MemU blog](https://memu.pro/blog/v0-vercel-ai-ui-memory)

---

## (f) The Reference UX Spec — What a 2026 Agent Control Plane Must Ship

Screens (non-negotiable baseline, synthesized from the matrix above):
1. **Session/chat surface** with streaming output, collapsible tool-call blocks, and inline diffs — table stakes across every product reviewed.
2. **Session list + history** (grid or sidebar) that survives restarts — Goose's `SessionListView`/`SessionHistoryView` split is a clean reference pattern.
3. **Multi-agent roster/manager** — parallel agents as first-class sidebar objects (Cursor, Grok Build, Antigravity, Warp all converged here independently in 2026; this is no longer optional).
4. **Diff/PR-style review pane** with accept/reject per hunk, not just per file (Cursor's unified multi-file diff, described unprompted by users as "a mini PR review").
5. **Permission/approval layer** with three explicit modes — Manual (ask every time), Auto (guardrailed), and a hard-stop spend/scope limit UI (Cline's "Spend Limit Reached" is the concrete pattern) — because permission fatigue is the single most-repeated 2026 complaint.
6. **Checkpoint/rewind with two restore granularities**: full revert vs. context-only compaction (Claude Code's `/rewind` menu), plus session fork so exploring an alternate path never destroys the original.
7. **Background/async execution with real notification**, not a bolt-on: native OS + mobile push on completion or on-needs-input, scoped per-agent (the gap so many third-party tools exist to fill is itself the spec).
8. **Extension/MCP marketplace** with a one-screen add flow (name, transport type, command/args) and support for official + third-party + private team marketplaces.
9. **Share/permalink surface**: turn any session or its output into a live, updating, shareable page (Claude Code Artifacts is the reference).
10. **Cost/token/spend dashboard**, per-session and aggregate, visible without leaving the tool (OpenHands Enterprise, Amp's context-window awareness).
11. **Trace/observability view for anyone building on the agent**, not just using it: click-a-node → detail-panel-on-the-right (LangSmith), pause/resume a live event stream, filter to failed-only (Temporal) — this is the pattern an agent control plane's own internals should expose to operators.
12. **Persistent, resumable execution environment per agent** (Factory's "persistent machine": installed packages, cloned repos, running services survive between turns) rather than re-provisioning from scratch each session.
13. **Cross-tool/cross-agent umbrella view** for power users running more than one agent product (Warp's single pane over Claude Code/Codex/Gemini CLI/OpenCode) — increasingly expected as the market fragments.
14. **Theming/customization surface** deep enough to export/share (Codex app's full theme editor) — a small thing that shows up repeatedly as a delight signal.

---

## Notes on gaps / things not found
- Could not verify a rewind/checkpoint or fork-session equivalent for Goose or Cursor in sourced material — flagged **[UNVERIFIED]** above rather than assumed absent.
- Could not verify a distinct "create a new named/persistent agent" flow (as opposed to starting a new session or writing a recipe/config) for Claude Code or Cursor.
- Screens marked "—" in the matrix reflect absence of a sourced claim, not confirmed absence of the feature.
