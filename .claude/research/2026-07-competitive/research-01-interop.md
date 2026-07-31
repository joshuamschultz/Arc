# Agent Harness Interoperability Landscape — 2026 Competitive Research

Research date: 2026-07-25. All claims cite an inline source URL; anything I could not verify from a primary/credible source is marked **[UNVERIFIED]**. Many 2026 "guide" sites are SEO/affiliate blogspam repeating each other — where a claim rests only on that tier of source, I've flagged it as **[LOW-CONFIDENCE SOURCE]** rather than presenting it as settled fact.

---

## (a) Product × Standard Support Matrix

| Product | MCP client | MCP server (agent-as-server) | A2A | ACP (Zed, editor) | AGENTS.md | SKILL.md | Apps SDK / MCP-UI | OTel GenAI | .well-known agent card |
|---|---|---|---|---|---|---|---|---|---|
| **Goose (Block)** | Yes, native [block.github.io/goose blog](https://block.github.io/goose/blog/2025/03/31/securing-mcp/) | Yes — goosed exposes REST+SSE and Goose can be wrapped as an MCP server for other agents [PulseMCP](https://www.pulsemcp.com/building-agents-with-goose/part-4-configure-your-agent-with-goose-recipes) | Bridged via community A2A↔MCP servers, not native [rapidclaw.dev](https://rapidclaw.dev/blog/a2a-protocol-complete-guide-2026) [UNVERIFIED strength] | No evidence found | Reads AGENTS.md per general 2026 convention adoption [buildbetter.ai](https://blog.buildbetter.ai/agents-md-complete-guide-for-engineering-teams-in-2026/) [UNVERIFIED specific] | Uses its own "recipe" YAML format, not SKILL.md natively [PulseMCP](https://www.pulsemcp.com/building-agents-with-goose/part-4-configure-your-agent-with-goose-recipes) | No evidence found | No evidence found | No evidence found |
| **Claude Code** | Yes, client + server [platform.claude.com](https://platform.claude.com/docs/en/agent-sdk/overview) / [docs.claude.com](https://docs.claude.com/en/docs/agent-sdk/mcp) | Yes | No native support found | Yes — beta support in Zed via ACP [zed.dev](https://zed.dev/blog/claude-code-via-acp) | Yes [codersera.com](https://codersera.com/blog/agents-md-vs-claude-md-vs-cursor-rules-comparison-2026/) | Yes, originator of the standard [agensi.io](https://www.agensi.io/learn/every-ai-agent-that-supports-skill-md-2026) | Compatible (ChatGPT built Apps SDK on MCP so the wire format overlaps) [vertu.com](https://vertu.com/ai-tools/openai-apps-sdk-mcp-agent-protocol-2026) | Not found directly claimed for Claude Code | No evidence found |
| **OpenCode (sst)** | Yes, client [PulseMCP](https://www.pulsemcp.com/clients/sst-opencode) | Yes — "code mode" MCP adapter [releasebot.io](https://releasebot.io/updates/sst/opencode) | No evidence found | Listed in the ACP Registry (28+ agents) [byteiota.com](https://byteiota.com/agent-client-protocol-lsp-ai-coding-agents/) | Aligned per general convention [codersera](https://codersera.com/blog/agents-md-vs-claude-md-vs-cursor-rules-comparison-2026/) | SKILL.md-compatible per broad list [agensi.io](https://www.agensi.io/learn/every-ai-agent-that-supports-skill-md-2026) [UNVERIFIED direct] | No evidence found | No evidence found | No evidence found |
| **Grok Build (xAI)** | Yes, native, explicitly reuses your Claude Code MCP config [buildfastwithai.com](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026) | No evidence found | No evidence found | No evidence found | Pulls in existing Claude Code config incl. AGENTS.md-adjacent setup [buildfastwithai.com](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026) [UNVERIFIED specifics] | Own "Skills" marketplace, separate from MCP [buildfastwithai.com](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026) — unclear if same file format as SKILL.md standard | No evidence found | No evidence found | No evidence found |
| **Pi (earendil-works, Mario Zechner)** | **Explicitly NOT built-in** — deliberately omitted from the minimal core [explainx.ai](https://explainx.ai/blog/pi-minimal-agent-harness-mario-zechner-guide-2026) | No | No | No evidence found | No evidence found | Ships "skills" as a core feature but minimal/custom [explainx.ai](https://explainx.ai/blog/pi-minimal-agent-harness-mario-zechner-guide-2026) | No | No | No |
| **OpenAI Codex / Codex CLI** | Yes, since GPT-5.5 (2026-04-23) [codegateway.dev](https://www.codegateway.dev/en/blog/openai-codex-cli-complete-guide-2026) | Underlies ChatGPT Apps SDK, which is MCP-based [vertu.com](https://vertu.com/ai-tools/openai-apps-sdk-mcp-agent-protocol-2026) | No evidence found | Listed in ACP Registry [byteiota.com](https://byteiota.com/agent-client-protocol-lsp-ai-coding-agents/); Zed shipped "Codex CLI in Zed 1.0" via ACP [codex.danielvaughan.com](https://codex.danielvaughan.com/2026/05/05/codex-cli-in-zed-parallel-agents-acp-integration-ide-workflows/) | Originator of the convention [thepromptshelf.dev](https://thepromptshelf.dev/blog/agents-md-codex-setup-guide-2026/) | Yes, compatible [agensi.io](https://www.agensi.io/learn/every-ai-agent-that-supports-skill-md-2026) | Yes, OpenAI's own product built on MCP [openai.com](https://openai.com/index/introducing-apps-in-chatgpt/) | No evidence found | No evidence found |
| **Google Gemini CLI / Antigravity** | Yes — remote MCP over streamable HTTP [ai.google.dev](https://ai.google.dev/gemini-api/docs/antigravity-agent) | No evidence found | Native — Google authored A2A; "full integration coming soon" per Google itself, i.e. not fully done yet [cloud.google.com](https://cloud.google.com/blog/topics/developers-practitioners/io26-news-for-agent-developers-on-google-cloud) | Yes — Gemini CLI was the reference implementation for Zed's ACP [zed.dev](https://zed.dev/blog/bring-your-own-agent-to-zed) | Aligned [codersera](https://codersera.com/blog/agents-md-vs-claude-md-vs-cursor-rules-comparison-2026/) | Compatible per broad list [agensi.io](https://www.agensi.io/learn/every-ai-agent-that-supports-skill-md-2026) | Not found directly | No evidence found | No evidence found |
| **Cursor** | Yes, per-agent server scoping, all surfaces [nxcode.io](https://www.nxcode.io/resources/news/cursor-mcp-servers-complete-guide-2026) | No evidence found | No evidence found | No evidence found (Cursor not named in Zed/ACP coverage) | Yes, aligned on the same format [morphllm.com](https://www.morphllm.com/agents-md-guide) | Compatible [agensi.io](https://www.agensi.io/learn/every-ai-agent-that-supports-skill-md-2026) | No evidence found | No evidence found | No evidence found |
| **Amp (Sourcegraph)** | Yes — Sourcegraph's own MCP server GA Feb 2026, plus Amp connects to it and to others [sourcegraph.com](https://sourcegraph.com/changelog/mcp-ga) | Sourcegraph MCP server acts as a server other agents connect to [sourcegraph.com/mcp](https://sourcegraph.com/mcp) | No evidence found | No evidence found | No evidence found | No evidence found | No evidence found | No evidence found | No evidence found |
| **Devin (Cognition)** | Yes [docs.devin.ai](https://docs.devin.ai/integrations/overview) | No evidence found | No evidence found | No evidence found | Listed among AGENTS.md adopters [buildbetter.ai](https://blog.buildbetter.ai/agents-md-complete-guide-for-engineering-teams-in-2026/) | No evidence found | No evidence found | No evidence found | No evidence found |
| **Cline** | Yes, plus own MCP Marketplace [evomap.ai](https://evomap.ai/blog/cline-mcp-servers-setup-guide-2026) | No evidence found | No evidence found | Listed among tools aligning w/ AGENTS.md/skills ecosystem, not directly confirmed for ACP | Aligned [blog.buildbetter.ai](https://blog.buildbetter.ai/agents-md-complete-guide-for-engineering-teams-in-2026/) | Yes [agensi.io](https://www.agensi.io/learn/every-ai-agent-that-supports-skill-md-2026); has own Skills+Plugins marketplace too [github.com/cline/cline CHANGELOG](https://github.com/cline/cline/blob/main/CHANGELOG.md) | No evidence found | No evidence found | No evidence found |
| **Aider** | **No** — no native MCP support as of v0.86.x, only an open RFC [morphllm.com](https://www.morphllm.com/comparisons/aider-vs-cline) | No | No | No | Listed as adopter [buildbetter.ai](https://blog.buildbetter.ai/agents-md-complete-guide-for-engineering-teams-in-2026/) | No evidence found | No | No | No |
| **OpenHands (ex-OpenDevin)** | Yes — native Streamable-HTTP + STDIO [deepwiki.com](https://deepwiki.com/OpenHands/software-agent-sdk/7.5-mcp-integration) | No evidence found | No evidence found | No evidence found | No evidence found | No evidence found | No evidence found | No evidence found | No evidence found |
| **OpenClaw** | Yes, per 2026 SKILL.md ecosystem coverage [agensi.io](https://www.agensi.io/learn/every-ai-agent-that-supports-skill-md-2026) | No evidence found | No evidence found | No evidence found | Yes, per general convention docs | Yes — arguably co-drove the standard's late-2025/2026 explosion [aiskill.market](https://aiskill.market/blog/convergence-openclaw-claude-code-unified-skill-layer) | No | No | No |
| **Hermes Agent (Nous Research)** | Works with "OpenAI-compatible endpoints" — provider-level, not confirmed MCP-tool-client [codersera.com](https://codersera.com/blog/hermes-agent-guide-to-multi-agent-ai-setup/) | No evidence found | No evidence found | No evidence found | No evidence found | Writes its own SKILL.md files autonomously [codersera.com](https://codersera.com/blog/hermes-agent-guide-to-multi-agent-ai-setup/) — same filename, unclear format-compatibility with the Anthropic-originated standard [aiskill.market](https://aiskill.market/blog/agentskills-io-standard-hermes-claude-code-cursor) notes "format-compatible rather than drop-in portable" | No | No | No |

Notes on the matrix:
- "ACP" is a genuinely overloaded acronym in 2026: **IBM's Agent Communication Protocol** (a REST-native agent-to-agent protocol) **merged into A2A** under the Linux Foundation in Sept 2025 [aimagicx.com](https://www.aimagicx.com/blog/mcp-vs-a2a-vs-acp-ai-agent-protocols-guide-2026); **Zed's Agent Client Protocol** is a *separate, unrelated* editor↔agent protocol (the "LSP for coding agents") [byteiota.com](https://byteiota.com/agent-client-protocol-lsp-ai-coding-agents/). The matrix column above is Zed's ACP, since that's the one with real 2026 traction (JetBrains adopting it across its whole IDE suite) [codex.danielvaughan.com](https://codex.danielvaughan.com/2026/05/05/codex-cli-in-zed-parallel-agents-acp-integration-ide-workflows/).
- Rows for **Pi** and **Hermes** confirm both exist as reported by the team lead: Pi = Mario Zechner's minimal harness at pi.dev / `earendil-works/pi`, MIT-licensed, ~67–70k GitHub stars as of July 2026, deliberately ships *without* MCP/subagents/plan-mode by design [explainx.ai](https://explainx.ai/blog/pi-minimal-agent-harness-mario-zechner-guide-2026); Hermes = Nous Research's self-hosted multi-agent framework, MIT, released Feb 2026, notable for multi-platform gateway (Telegram/Discord/Slack/WhatsApp) and self-writing SKILL.md [codersera.com](https://codersera.com/blog/hermes-agent-guide-to-multi-agent-ai-setup/).

---

## (b) Per-Product Notes — Method of Integration

### Block's Goose — the benchmark
- **Two-process architecture**: the `goose` CLI is an in-process agent for interactive/headless use; `goosed` is a separate server process that wraps the same Agent behind a bespoke **REST + SSE HTTP API with ~103 endpoints**, and the desktop app spawns `goosed` as a child process [dosu.dev summary of GitHub discussion #7697](https://app.dosu.dev/e3630b91-3a35-46b9-a8d3-b0c1b3ef6331/documents/f55313d1-d0da-4357-b6ed-bb8b5eeb92da).
- **Headless/CI mode**: `goose run --no-session -t "<task>"` [pulsemcp.com](https://www.pulsemcp.com/building-agents-with-goose/part-4-configure-your-agent-with-goose-recipes).
- **Agent-as-MCP-server**: a secure MCP server lets *other* AI agents drive the Goose CLI to automate coding tasks on a project folder — i.e., Goose can be wrapped and exposed as a tool to another agent, not just consume tools itself [pulsemcp.com](https://www.pulsemcp.com/building-agents-with-goose/part-4-configure-your-agent-with-goose-recipes).
- **Subagents via Recipes**: a Recipe is a shareable YAML document bundling instructions, required extensions/MCP servers, parameters, and prompt; sub-recipes let a main recipe delegate to specialized recipes run as parallel or sequential subagents [pulsemcp.com](https://www.pulsemcp.com/building-agents-with-goose); community MCP servers exist purely to let external clients "delegate tasks to autonomous developer teams using Goose CLI subagents" [glama.ai](https://glama.ai/mcp/servers/@pc-style/goose-mcp).
- **Governance shift**: Goose moved from a Block-internal project to the Linux Foundation's Agentic AI Foundation (AAIF) in 2026 [baeseokjae.github.io](https://baeseokjae.github.io/posts/goose-ai-agent-review-2026/) — same foundation now stewarding AGENTS.md, A2A, and (per some sources) MCP-adjacent governance.
- **License**: Apache 2.0, no commercial-use restriction [openaitoolshub.org](https://www.openaitoolshub.org/en/blog/goose-ai-agent-block-review).

### Claude Code (Anthropic)
- **Headless mode**: `claude -p` (`--print`) runs one turn and exits; `--output-format text|json|stream-json`; reads stdin, pre-approves tools via `--allowedTools`/`--permission-mode` for unattended runs [amux.io](https://amux.io/guides/claude-code-headless/), [ocdevel.com](https://ocdevel.com/podcaster/claude-code/7fdc1bc3-0a3b-42a3-8b68-c7e5f61d6b38).
- **Streaming protocol**: `stream-json` emits one self-contained JSON object per line; opens with a `system`/`init` event reporting session ID, model, available tools, MCP servers, and loaded plugins; recently added `mcp_server_errors` in that init event and `--forward-subagent-text` for nested (depth-2+) subagent output [ocdevel.com](https://ocdevel.com/podcaster/claude-code/7fdc1bc3-0a3b-42a3-8b68-c7e5f61d6b38).
- **Claude Agent SDK**: `@anthropic-ai/claude-agent-sdk` (TS) / `claude-agent-sdk` (Python ≥3.10) wraps the same engine as a library for building your own products on top of it [morphllm.com](https://www.morphllm.com/claude-agent-sdk). Subagents are defined via `AgentDefinition` (description + prompt + tool list) and spawned through the `Agent` tool [ksred.com](https://www.ksred.com/the-claude-agent-sdk-what-it-is-and-why-its-worth-understanding/); MCP servers are wired in via `mcp_servers`/`mcpServers` config [docs.claude.com](https://docs.claude.com/en/docs/agent-sdk/mcp).
- **Editor interop**: beta support for running Claude Code *inside* Zed via ACP, alongside Gemini CLI and Codex CLI in the same window [zed.dev](https://zed.dev/blog/claude-code-via-acp).
- **CI/PR bot**: `anthropics/claude-code-action@v1` reached GA in early 2026 — `@claude` mentions in PRs/issues trigger review, fixes, feature implementation; built-in guardrails against runaway loops [systemprompt.io](https://systemprompt.io/guides/claude-code-github-actions), [groundy.com](https://groundy.com/articles/how-to-run-claude-code-as-a-github-actions-agent-for-automated-pr-fixes/).
- **Plugins**: a plugin is a directory with `.claude-plugin/plugin.json` (name, version, author); a `.claude-plugin/marketplace.json` turns any git repo into a plugin marketplace; Anthropic curates a reviewed directory; updates are pull-based (`/plugin marketplace update`) — no cryptographic signing requirement was found in docs [github.com/anthropics/claude-plugins-official](https://github.com/anthropics/claude-plugins-official), [code.claude.com/docs/en/plugins-reference](https://code.claude.com/docs/en/plugins-reference). **[No signing found — likely a real gap, see §(f)]**
- **Skills**: originated the Agent Skills open standard (SKILL.md) that now underlies most of the industry's skill portability story — see §(c) timeline below.

### OpenCode (sst)
- **Architecture**: Bun/TypeScript client-server; TUI front end; real-time collaboration via Cloudflare Workers + Durable Objects [ssntpl.com](https://ssntpl.com/opencode-open-source-ai-coding-agent-guide/).
- **MCP**: connects to local and remote MCP servers (GitHub, Postgres, Slack, custom); v1.17.10 added MCP *resources* (template listing + resource-read tools) and now surfaces MCP server instructions directly in session context; also shipped a "code mode" MCP adapter for running confined orchestration scripts against connected MCP tools [releasebot.io](https://releasebot.io/updates/sst/opencode).
- **LSP**: comprehensive LSP client support baked in for large-codebase navigation [ssntpl.com](https://ssntpl.com/opencode-open-source-ai-coding-agent-guide/).
- Listed in the **ACP Registry** (Zed/JetBrains) alongside Claude Code, Codex CLI, GitHub Copilot CLI, Gemini CLI [byteiota.com](https://byteiota.com/agent-client-protocol-lsp-ai-coding-agents/).

### Grok Build (xAI)
- Shipped 2026-05-14 (beta opened to SuperGrok/X Premium+ 2026-05-25); terminal-native, up to **8 parallel sub-agents racing the same problem**, plan-first loop, underlying model `grok-build-0.1` at $0.20/M input tokens [buildfastwithai.com](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026).
- **Notable interop claim**: "any MCP server you've already wired up for Claude Code ... works with Grok Build with zero reconfiguration" — it explicitly imports/reuses existing Claude Code MCP config, custom skills, and MCP server registrations [buildfastwithai.com](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026). This is a deliberate free-ride on Claude Code's installed base — worth tracking as a competitive pattern (piggyback on the incumbent's config rather than building your own registry).
- Ships its own **Skills marketplace** as the "native, deeper" extension mechanism, explicitly distinct from MCP ("use MCP for cross-tool compat, Skills for deeper Grok Build integration") [buildfastwithai.com](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026).

### "Pi" (Mario Zechner / Earendil Inc., pi.dev)
- This is a **real, distinct product** from Perplexity's "Pi" or Inflection's "Pi" — it's `@earendil-works/pi-coding-agent`, a minimal open-source coding harness. ~67–70k GitHub stars, MIT license, v0.78.0 as of May 2026, reached #1 on Hacker News (608 points) [explainx.ai](https://explainx.ai/blog/pi-minimal-agent-harness-mario-zechner-guide-2026), [academy.kspl.tech](https://academy.kspl.tech/blog/2026-06-05-pi-agent-deep-dive-2026).
- **Philosophy is the opposite of Goose/Claude Code**: it deliberately ships a 4-tool core (Read/Write/Edit/Bash) and **omits MCP, subagents, plan mode, permission popups, built-in todos, and background bash** — everything else is an in-process TypeScript "extension" that registers tools, rewrites context, or redraws the UI [explainx.ai](https://explainx.ai/blog/pi-minimal-agent-harness-mario-zechner-guide-2026).
- Monorepo split: `pi-coding-agent` (CLI), `pi-agent-core` (runtime/tool-calling/state), `pi-ai` (unified multi-provider LLM API — Anthropic/OpenAI/Google/Azure/Bedrock/Mistral/Groq/Cerebras/xAI/HF/OpenRouter/Ollama), `pi-tui` (terminal UI) [buildfastwithai.com-adjacent source, academy.kspl.tech](https://academy.kspl.tech/blog/2026-06-05-pi-agent-deep-dive-2026).
- Notably **powers OpenClaw's underlying agent loop** per one source [academy.kspl.tech](https://academy.kspl.tech/blog/2026-06-05-pi-agent-deep-dive-2026) — this needs independent confirmation; flagging as **[UNVERIFIED — single source]** since it's a load-bearing claim about a 280k-star project's internals.
- **Interop takeaway**: Pi is the "reference harness" case study — it proves you can be taken seriously in 2026 with *zero* of the standard protocols, as long as you're radically transparent about the wiring and let extensions add whatever the user needs. It is the strongest evidence against "you must ship MCP+A2A+ACP to matter," but it also has essentially no enterprise/team distribution story yet.

### OpenAI Codex / Codex CLI
- **Headless**: `codex exec` — non-interactive, single-session-to-completion, emits events to stdout/stderr [deepwiki.com](https://deepwiki.com/openai/codex/4.2-headless-execution-mode-(codex-exec)).
- **MCP**: shipped alongside GPT-5.5 (2026-04-23) [codegateway.dev](https://www.codegateway.dev/en/blog/openai-codex-cli-complete-guide-2026).
- **Multi-agent**: experimental support for running multiple agents in parallel across isolated git worktrees [codegateway.dev](https://www.codegateway.dev/en/blog/openai-codex-cli-complete-guide-2026).
- **AGENTS.md**: Codex is the format's origin point; OpenAI later donated it to the Agentic AI Foundation (Linux Foundation) for neutral governance, alongside Anthropic and Block as founding members [blog.buildbetter.ai](https://blog.buildbetter.ai/agents-md-complete-guide-for-engineering-teams-in-2026/).
- **Apps SDK**: built directly on MCP; "a tool or resource that works with one MCP-compliant client works with the others" [vertu.com](https://vertu.com/ai-tools/openai-apps-sdk-mcp-agent-protocol-2026); ChatGPT implements the **MCP Apps UI standard** so a UI built once runs across MCP-Apps-compatible hosts [openai.com](https://openai.com/index/introducing-apps-in-chatgpt/). As of 2026-07-09 the "app directory" was folded into a unified "Plugin directory" spanning ChatGPT and Codex [openai.com source cited in search summary].
- **Zed/ACP**: "Codex CLI in Zed 1.0" ships parallel-agent support via ACP [codex.danielvaughan.com](https://codex.danielvaughan.com/2026/05/05/codex-cli-in-zed-parallel-agents-acp-integration-ide-workflows/).

### Google Gemini CLI / Antigravity
- **Major 2026 discontinuity**: Gemini CLI is being **deprecated** in favor of "Antigravity 2" (desktop IDE) and a new **Antigravity CLI**, effective 2026-06-18 for Pro/Ultra accounts [en.wikipedia.org/Google_Antigravity summarized in search](https://blog.google/innovation-and-ai/technology/developers-tools/google-io-2026-developer-highlights/). Anyone benchmarking against "Gemini CLI" needs to track this migration — it's a moving target.
- **MCP**: Antigravity agent connects to remote MCP servers over streamable HTTP [ai.google.dev](https://ai.google.dev/gemini-api/docs/antigravity-agent).
- **A2A**: Google originated A2A; its own I/O 2026 messaging says "full integration with A2A and Agent Platform governance and security are **coming soon**" [cloud.google.com](https://cloud.google.com/blog/topics/developers-practitioners/io26-news-for-agent-developers-on-google-cloud) — i.e., even the protocol's author hadn't fully shipped it in its own flagship agent product as of mid-2026. Notable gap.
- **ACP**: Gemini CLI was Zed's **reference implementation partner** for launching ACP [zed.dev](https://zed.dev/blog/bring-your-own-agent-to-zed).

### Cursor
- MCP servers scoped per-agent, across all surfaces (chat, background agents, CLI) [nxcode.io](https://www.nxcode.io/resources/news/cursor-mcp-servers-complete-guide-2026).
- **Headless CLI**: Node.js 18+, shares MCP servers/rules/auth with the desktop app; stable on macOS/Linux/Windows (2026); `--headless` flag for CI/CD [deploy HQ + nxcode summaries](https://www.deployhq.com/guides/cursor).
- **Background Agents**: cloud-sandboxed agents that run while the user does other work — this is Cursor's distinct "drives without a human" surface, comparable to Devin's async model [deployhq.com](https://www.deployhq.com/guides/cursor).
- No evidence of Cursor participating in A2A, ACP-as-a-provider, or exposing itself as an MCP server.

### Amp (Sourcegraph)
- Sourcegraph ships its **own MCP server**, GA as of 2026-02-25, with frictionless OAuth (Dynamic Client Registration) enabled by default [sourcegraph.com/changelog/mcp-dcr](https://sourcegraph.com/changelog/mcp-dcr), [sourcegraph.com/changelog/mcp-ga](https://sourcegraph.com/changelog/mcp-ga). This lets Amp, Claude Code, VS Code, or Cursor all connect to a Sourcegraph instance through one server, with different specialized tool-suite endpoints (including a "Deep Search" subagent endpoint) [sourcegraph.com/mcp](https://sourcegraph.com/mcp).
- Amp itself decomposes tasks into parallel subagent branches, either on explicit instruction or automatically when it detects independent subtasks [baeseokjae.github.io](https://baeseokjae.github.io/posts/amp-code-review-2026/) [LOW-CONFIDENCE SOURCE].
- Rapid adoption claim: 40,000+ teams in first two months of 2026 [same source, LOW-CONFIDENCE].

### Devin (Cognition)
- Primary interfaces are **Slack** (assign tasks via message, Devin replies/asks questions in-thread) and **GitHub** (works issues/PRs directly) — chat-platform-first rather than terminal-first [docs.devin.ai](https://docs.devin.ai/integrations/overview).
- **MCP**: native support for "hundreds" of external tools/data sources, explicitly including Sentry/Datadog/Vercel log access via MCP [docs.devin.ai](https://docs.devin.ai/integrations/overview).
- Third-party **MCP servers that wrap Devin itself** exist (e.g., a Devin↔Slack bridge MCP server) so other agents can drive Devin sessions and get task/thread context back [mcpmarket.com](https://mcpmarket.com/server/devin-slack-integration), [skywork.ai](https://skywork.ai/skypage/en/devin-mcp-server-ai-engineers-guide/1978707753774469120). This is Devin's version of "agent as MCP server," but via community wrapper rather than an official Cognition-shipped server.

### Cline
- VS Code-native, now also JetBrains, Cursor, Windsurf, Zed, Neovim, plus a preview CLI (macOS/Linux) [vibecodinghub.org](https://vibecodinghub.org/tools/cline).
- **MCP Marketplace**: dedicated in-product marketplace, filterable by installs/stars/date/category; supports both stdio and SSE transports [evomap.ai](https://evomap.ai/blog/cline-mcp-servers-setup-guide-2026).
- **Subagents**: read-only parallel research agents, each with their own context window; explicitly **cannot** write files, run destructive commands, or access MCP servers — a deliberate blast-radius limitation [github.com/cline/cline CHANGELOG](https://github.com/cline/cline/blob/main/CHANGELOG.md).
- **Plugins**: "Cline Plugins" let teams bundle custom tools/workflows/skills/MCP capabilities together — same "plugin wraps multiple primitives" pattern as Claude Code plugins [github.com/cline/cline CHANGELOG](https://github.com/cline/cline/blob/main/CHANGELOG.md).

### Aider
- **The clearest MCP laggard among mainstream tools**: as of v0.86.x, no native MCP — an RFC (#4506) is open, an exploratory PR (#3937) was closed without merging [morphllm.com](https://www.morphllm.com/comparisons/aider-vs-cline). Workarounds are ad hoc: `/web`, `/run` (shell), `/read-only`, or third-party MCP-compatible search tools like WarpGrep that happen to also work with Aider [morphllm.com](https://www.morphllm.com/comparisons/aider-vs-cline).
- Its differentiator remains **architect/editor model-pairing** (strong model proposes, cheap model edits) rather than protocol breadth [morphllm.com](https://www.morphllm.com/comparisons/aider-vs-cline).
- Config-as-code via `.aider.conf.yml` [morphllm.com](https://www.morphllm.com/comparisons/aider-vs-cline).

### OpenHands (formerly OpenDevin)
- **MCP**: native Streamable-HTTP support, plus STDIO subprocess transport [deepwiki.com](https://deepwiki.com/OpenHands/software-agent-sdk/7.5-mcp-integration). Note the timeline conflict in sources — one April 2026 source says MCP wasn't yet implemented, a later one confirms native support shipped; treat as "shipped by mid-2026, immature earlier in the year."
- **Headless**: scriptable headless mode alongside interactive CLI; documented best practice to set `MAX_ITERATIONS`, `LLM_NUM_RETRIES`, and a hard cost ceiling when running headless (a real operational gotcha for anyone running it unattended) [deepwiki.com](https://deepwiki.com/OpenHands/software-agent-sdk/7.5-mcp-integration) [toolhalla.ai](https://toolhalla.ai/blog/devin-vs-openhands-vs-swe-agent-2026).
- **Chat/tracker integrations**: documented Slack, Jira, Linear, and a GitHub App [deepwiki.com](https://deepwiki.com/All-Hands-AI/OpenHands/13.6-third-party-integrations-(slack-jira-linear-github-app)).
- Explicit security framing in its own docs: MCP servers run with the agent's privileges and should be pinned/audited like any other dependency [deepwiki.com](https://deepwiki.com/OpenHands/software-agent-sdk/7.5-mcp-integration).

### OpenClaw
- Origin story: "Clawdbot" (Nov 2025) → "Moltbot" (2026-01-27) → "OpenClaw" (2026-01-30, after a trademark dispute with Anthropic) [clawbot.blog](https://www.clawbot.blog/blog/openclaw-the-ai-agent-framework-explained-april-2026-update/). 280,000+ GitHub stars, reportedly the most-starred repo in history, overtaking React [clawbot.blog](https://www.clawbot.blog/blog/openclaw-the-ai-agent-framework-explained-april-2026-update/).
- 26 built-in tools spanning filesystem, shell, web search/fetch, browser automation, memory, session control [clawbot.blog](https://www.clawbot.blog/blog/openclaw-the-ai-agent-framework-explained-april-2026-update/).
- "Task Brain" control panel (2026.3.31 beta) is described as the biggest architectural shift since its original heartbeat design [clawbot.blog](https://www.clawbot.blog/blog/openclaw-the-ai-agent-framework-explained-april-2026-update/).
- **Security concern of note**: an arXiv paper specifically analyzes OpenClaw's security posture, and one MCP-security piece cites "the OpenClaw attacker escaped Docker" as a cautionary example that container sandboxing alone is insufficient [nimblebrain.ai-class source cited in search summary](https://arxiv.org/pdf/2603.27517). Given OpenClaw's massive install base, this is a material data point on sandboxing maturity across the ecosystem, not just OpenClaw specifically.
- SKILL.md: OpenClaw is described as co-driving the "convergence" of the skill format alongside Claude Code [aiskill.market](https://aiskill.market/blog/convergence-openclaw-claude-code-unified-skill-layer).

### Hermes Agent (Nous Research)
- Released Feb 2026, MIT license, self-hosted [codersera.com](https://codersera.com/blog/hermes-agent-guide-to-multi-agent-ai-setup/).
- **Distinctive interop surface**: one gateway, multiple chat platforms — terminal, Telegram, Discord, Slack, WhatsApp [codersera.com](https://codersera.com/blog/hermes-agent-guide-to-multi-agent-ai-setup/). This is a genuinely different integration axis than the IDE/CLI-centric tools above — Hermes competes on *where humans already are* rather than on protocol breadth.
- **Multi-agent**: "Worktree Mode" — a Primary Agent spawns multiple subagents simultaneously, each in an isolated context/branch, explicitly to prevent context pollution [codersera.com](https://codersera.com/blog/hermes-agent-guide-to-multi-agent-ai-setup/). Reported 75–85% success on complex network-design tasks in a benchmark study vs. chain-of-thought baselines [codersera.com](https://codersera.com/blog/hermes-agent-guide-to-multi-agent-ai-setup/) [LOW-CONFIDENCE SOURCE — no link to the underlying study found].
- **Self-writing skills**: writes its own SKILL.md as it learns workflows, but format-compatibility with the Anthropic-originated standard is only partial ("format-compatible rather than drop-in portable") [aiskill.market](https://aiskill.market/blog/agentskills-io-standard-hermes-claude-code-cursor).
- No MCP-client or A2A/ACP evidence found — model access is via OpenAI-compatible endpoints, which is a *model*-layer compatibility, not a *tool/agent*-layer one. **This looks like Hermes's real interop gap.**

---

## (c) What's Table Stakes in 2026

1. **MCP client support** — near-universal; the one clear laggard among mainstream tools is Aider (no native support, open RFC since at least early 2026) [morphllm.com](https://www.morphllm.com/comparisons/aider-vs-cline). Pi omits it by explicit design choice, not oversight.
2. **AGENTS.md (or equivalent project-instruction file) support** — reads it or something like it; the format itself is now foundation-governed (Linux Foundation's Agentic AI Foundation, founded Dec 2025 by OpenAI/Anthropic/Block) with 60,000+ repos using it [blog.buildbetter.ai](https://blog.buildbetter.ai/agents-md-complete-guide-for-engineering-teams-in-2026/).
3. **SKILL.md-compatible skill loading** — 15–30+ tools claim compatibility per 2026 trackers, though "compatible" ranges from byte-identical to "parses the frontmatter, ignores runtime-specific metadata" [agensi.io](https://www.agensi.io/learn/every-ai-agent-that-supports-skill-md-2026), [aiskill.market](https://aiskill.market/blog/agentskills-io-standard-hermes-claude-code-cursor).
4. **Headless / non-interactive exec mode with structured (JSON/stream-JSON) output** — every serious CLI tool in this list has one (Claude Code `-p`, Codex `exec`, Goose `run --no-session`, Cursor `--headless`, OpenHands headless mode).
5. **Subagent / parallel-task delegation** — present in Goose, Claude Code, Grok Build, Amp, Cline (read-only), Hermes; absent (by design) in Pi and Aider.
6. **Some GitHub integration** (PR review, issue-to-PR, CI bot) — Claude Code, Devin, Goose, OpenHands, Codex all have this; it's close to mandatory for "serious coding agent" credibility.

## (d) What's Differentiating in 2026

1. **Agent-as-MCP-server / exposing yourself as infrastructure** — only Goose (goosed REST+SSE, community MCP wrappers) and, informally, Devin (via community MCP bridge) clearly do this. Most tools are MCP *consumers* only. This is a real gap for anyone wanting to be embedded inside other agents' workflows rather than just consuming their tools.
2. **A2A depth** — Google authored it and still says its own flagship product's "full integration" is "coming soon" [cloud.google.com](https://cloud.google.com/blog/topics/developers-practitioners/io26-news-for-agent-developers-on-google-cloud); most other A2A mentions in this research are bridges/demos, not production paths. Real A2A depth would be a genuine differentiator, not table stakes, in mid-2026.
3. **Editor-agnostic driving via ACP (Zed's)** — Claude Code, Gemini CLI, Codex CLI, OpenCode all participate; Cursor and Amp show no evidence of doing so. JetBrains committing to ACP across its whole IDE suite is a meaningful 2026 signal, but Microsoft/VS Code has explicitly not committed ("MCP covers their needs") [codex.danielvaughan.com](https://codex.danielvaughan.com/2026/05/05/codex-cli-in-zed-parallel-agents-acp-integration-ide-workflows/).
4. **Multi-channel human touchpoints beyond IDE/terminal** — Devin (Slack-first) and Hermes (Telegram/Discord/Slack/WhatsApp gateway) stand out; most coding agents are still IDE/terminal-bound.
5. **Reusing a competitor's installed config as a distribution shortcut** — Grok Build explicitly imports existing Claude Code MCP/skill config "with zero reconfiguration" [buildfastwithai.com](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026). This is a low-cost, high-leverage interop move worth studying, not just table stakes.
5. **Radical minimalism as a trust strategy** — Pi's bet that zero built-in protocol surface + full extension transparency builds more trust than protocol breadth. Differentiating, but unproven at team/enterprise scale.
6. **OpenTelemetry GenAI semconv emission** — draft/experimental across the board; adopted more by observability *vendors* (Datadog, Honeycomb, New Relic) and orchestration *frameworks* (LangChain, CrewAI, AutoGen) than by the coding-agent products in this matrix directly [datadoghq.com](https://www.datadoghq.com/blog/llm-otel-semantic-convention/), [greptime.com](https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions). No coding-agent-specific OTel GenAI claim was found for any product in this matrix — this is a real, current whitespace.

## (e) Ranked Top-10 Interop Capabilities a New Entrant MUST Have (2026)

1. **MCP client (consume tools)** — table stakes; near-zero credibility without it. *Difficulty: Low* — spec is mature, SDKs exist in every major language.
2. **Headless/non-interactive exec mode with structured JSON/stream-JSON output** — required for any CI, automation, or "another agent drives me" use case. *Difficulty: Low-Medium* — mostly an engineering/API-design task, not a research problem.
3. **AGENTS.md (or declared-compatible) project-context file support** — expected default; foundation-governed now, so implementing it also buys governance-neutrality goodwill. *Difficulty: Low.*
4. **SKILL.md-compatible skill loading** — the closest thing to a converged "extension format" across 15–30+ tools; skipping it isolates you from a fast-growing shared skill ecosystem (89,000+ skills on one marketplace alone per one source [aiskill.market timeline]). *Difficulty: Low-Medium* — parsing is easy; the harder part is deciding how much runtime-specific metadata to also support for parity.
5. **Subagent / parallel task delegation** — expected of any harness positioning itself as more than a single-threaded assistant; Pi is the sole credible counterexample and it accepts that tradeoff explicitly. *Difficulty: Medium* — needs isolated context/state management and a delegation UX, not just a protocol.
6. **GitHub PR/issue automation (bot or Action)** — the single most common "prove it in production" surface teams evaluate against. *Difficulty: Medium* — needs careful guardrails against runaway loops/cost, which Claude Code and OpenHands both call out explicitly.
7. **Agent-as-MCP-server (be embeddable inside other agents' workflows)** — currently a real differentiator (only Goose does it natively); becoming necessary as more orchestration happens agent-to-agent rather than human-to-agent. *Difficulty: Medium-High* — requires a stable server-side API/auth model, not just a wrapper script.
8. **ACP (Zed) or equivalent editor-embedding protocol** — matters if you want IDE distribution (Zed + JetBrains) without building N editor plugins yourself. *Difficulty: Medium* — protocol is simpler than MCP but still requires a persistent-session, streaming-capable server mode.
9. **A2A support (even partial/bridged)** — currently mostly aspirational even for its own author (Google), but the Linux-Foundation-governed convergence trend (ACP-IBM merged into A2A, AAIF governance) makes it likely to matter more within 12–18 months; getting a basic bridge working now is cheap insurance. *Difficulty: Medium* — spec is more complex than MCP (discovery cards, task lifecycle, streaming), but "bridge only" implementations already exist as a shortcut.
10. **Signed/verifiable extension supply chain (plugin, MCP server, or skill signing + sandboxing)** — this is the one capability almost nobody in this matrix has fully solved; NSA/Microsoft/OWASP 2026 guidance all call out unsigned MCP registries and Docker-escape-class sandbox failures as live risks [nsa.gov CSI](https://www.nsa.gov/Portals/75/documents/Cybersecurity/CSI_MCP_SECURITY.pdf), [techcommunity.microsoft.com](https://techcommunity.microsoft.com/blog/microsoft-security-blog/the-state-of-mcp-security-in-2026/4531327). Shipping real cryptographic provenance + sandboxing (not just "we support MCP") is the capability most likely to be a genuine, defensible differentiator for a federal-postured entrant like Arc. *Difficulty: High* — needs a signing/verification pipeline, a trust root, and enforced sandboxing (seccomp/AppArmor-class isolation, not just containers), which is exactly the ground Arc's Four Pillars (Identity/Sign/Authorize/Audit) already stake out.

## (f) Notable Weak Spots / Gaps Observed

- **No product in this research was found to emit OpenTelemetry GenAI semantic-convention spans natively as a coding agent** — adoption is at the framework/observability-vendor layer, not the agent-product layer. Real whitespace.
- **Plugin/extension signing is largely unaddressed.** Claude Code's plugin docs describe manifest structure and semver but no cryptographic signing requirement was found. MCP registry security research explicitly flags unsigned "pointer architecture" registries and that 15.4% of registry servers ship with no visible source code [source cited above].
- **Google's own A2A integration in its flagship agent product (Antigravity) is admittedly incomplete** ("coming soon") as of mid-2026 — a good illustration that "authored the standard" ≠ "fully implements the standard."
- **Aider is the clearest mainstream MCP holdout**, relying on RFC discussion rather than a shipped integration.
- **Two different, unrelated protocols are both called "ACP"** in the wild (IBM's now-merged-into-A2A Agent Communication Protocol, and Zed's still-independent Agent Client Protocol) — worth being precise about in any roadmap doc to avoid confusing readers or reviewers.
- Several 2026 "guide" sites used as sources here are SEO/affiliate content that summarize primary docs (or each other) rather than being primary sources themselves; flagged inline as LOW-CONFIDENCE where a claim rests solely on that tier.

---

### Sources index (all URLs used above, deduplicated)
- https://onereach.ai/blog/guide-choosing-mcp-vs-a2a-protocols/
- https://zylos.ai/research/2026-03-26-agent-interoperability-protocols-mcp-a2a-acp-convergence/
- https://app.dosu.dev/e3630b91-3a35-46b9-a8d3-b0c1b3ef6331/documents/f55313d1-d0da-4357-b6ed-bb8b5eeb92da
- https://block.github.io/goose/blog/2025/03/31/securing-mcp/
- https://www.pulsemcp.com/building-agents-with-goose/part-4-configure-your-agent-with-goose-recipes
- https://www.pulsemcp.com/building-agents-with-goose
- https://glama.ai/mcp/servers/@pc-style/goose-mcp
- https://baeseokjae.github.io/posts/goose-ai-agent-review-2026/
- https://www.openaitoolshub.org/en/blog/goose-ai-agent-block-review
- https://ocdevel.com/podcaster/claude-code/7fdc1bc3-0a3b-42a3-8b68-c7e5f61d6b38
- https://amux.io/guides/claude-code-headless/
- https://www.morphllm.com/claude-agent-sdk
- https://www.ksred.com/the-claude-agent-sdk-what-it-is-and-why-its-worth-understanding/
- https://docs.claude.com/en/docs/agent-sdk/mcp
- https://platform.claude.com/docs/en/agent-sdk/overview
- https://zed.dev/blog/claude-code-via-acp
- https://systemprompt.io/guides/claude-code-github-actions
- https://groundy.com/articles/how-to-run-claude-code-as-a-github-actions-agent-for-automated-pr-fixes/
- https://github.com/anthropics/claude-plugins-official
- https://code.claude.com/docs/en/plugins-reference
- https://ssntpl.com/opencode-open-source-ai-coding-agent-guide/
- https://www.pulsemcp.com/clients/sst-opencode
- https://releasebot.io/updates/sst/opencode
- https://byteiota.com/agent-client-protocol-lsp-ai-coding-agents/
- https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026
- https://explainx.ai/blog/pi-minimal-agent-harness-mario-zechner-guide-2026
- https://academy.kspl.tech/blog/2026-06-05-pi-agent-deep-dive-2026
- https://blog.buildbetter.ai/agents-md-complete-guide-for-engineering-teams-in-2026/
- https://thepromptshelf.dev/blog/agents-md-codex-setup-guide-2026/
- https://www.morphllm.com/agents-md-guide
- https://codersera.com/blog/agents-md-vs-claude-md-vs-cursor-rules-comparison-2026/
- https://deepwiki.com/openai/codex/4.2-headless-execution-mode-(codex-exec)
- https://www.codegateway.dev/en/blog/openai-codex-cli-complete-guide-2026
- https://vertu.com/ai-tools/openai-apps-sdk-mcp-agent-protocol-2026
- https://openai.com/index/introducing-apps-in-chatgpt/
- https://help.openai.com/en/articles/12584461-developer-mode-and-mcp-apps-in-chatgpt
- https://cloud.google.com/blog/topics/developers-practitioners/io26-news-for-agent-developers-on-google-cloud
- https://ai.google.dev/gemini-api/docs/antigravity-agent
- https://blog.google/innovation-and-ai/technology/developers-tools/google-io-2026-developer-highlights/
- https://zed.dev/blog/bring-your-own-agent-to-zed
- https://www.deployhq.com/guides/cursor
- https://www.nxcode.io/resources/news/cursor-mcp-servers-complete-guide-2026
- https://baeseokjae.github.io/posts/amp-code-review-2026/
- https://sourcegraph.com/changelog/mcp-ga
- https://sourcegraph.com/changelog/mcp-dcr
- https://sourcegraph.com/mcp
- https://docs.devin.ai/integrations/overview
- https://mcpmarket.com/server/devin-slack-integration
- https://skywork.ai/skypage/en/devin-mcp-server-ai-engineers-guide/1978707753774469120
- https://vibecodinghub.org/tools/cline
- https://evomap.ai/blog/cline-mcp-servers-setup-guide-2026
- https://github.com/cline/cline/blob/main/CHANGELOG.md
- https://www.morphllm.com/comparisons/aider-vs-cline
- https://deepwiki.com/OpenHands/software-agent-sdk/7.5-mcp-integration
- https://deepwiki.com/All-Hands-AI/OpenHands/13.6-third-party-integrations-(slack-jira-linear-github-app)
- https://toolhalla.ai/blog/devin-vs-openhands-vs-swe-agent-2026
- https://www.clawbot.blog/blog/openclaw-the-ai-agent-framework-explained-april-2026-update/
- https://codersera.com/blog/hermes-agent-guide-to-multi-agent-ai-setup/
- https://aiskill.market/blog/convergence-openclaw-claude-code-unified-skill-layer
- https://aiskill.market/blog/agentskills-io-standard-hermes-claude-code-cursor
- https://www.agensi.io/learn/every-ai-agent-that-supports-skill-md-2026
- https://a2a-protocol.org/latest/topics/agent-discovery/
- https://github.com/a2aproject/A2A/blob/main/docs/topics/agent-discovery.md
- https://tyk.io/learning-center/a2a-protocol-architecture-and-technical-specification/
- https://www.datadoghq.com/blog/llm-otel-semantic-convention/
- https://opentelemetry.io/blog/2025/ai-agent-observability/
- https://greptime.com/blogs/2026-05-09-opentelemetry-genai-semantic-conventions
- https://www.aimagicx.com/blog/mcp-vs-a2a-vs-acp-ai-agent-protocols-guide-2026
- https://neosalpha.com/blogs/ai-agent-protocols-acp-vs-mcp-vs-a2a/
- https://tessl.io/blog/zed-debuts-agent-client-protocol-to-connect-ai-coding-agents-to-any-editor/
- https://zed.dev/acp
- https://codex.danielvaughan.com/2026/05/05/codex-cli-in-zed-parallel-agents-acp-integration-ide-workflows/
- https://www.nsa.gov/Portals/75/documents/Cybersecurity/CSI_MCP_SECURITY.pdf
- https://techcommunity.microsoft.com/blog/microsoft-security-blog/the-state-of-mcp-security-in-2026/4531327
- https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html
