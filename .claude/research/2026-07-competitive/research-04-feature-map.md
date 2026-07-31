# 2026 Agent-Harness Feature Map

**Date compiled:** 2026-07-25. Benchmark product: **Block Goose**. Every non-obvious claim carries a source URL; anything I could not verify is flagged **[UNVERIFIED]**. Gaps are stated honestly rather than filled with plausible guesses — this market moves weekly and several products (Grok Build, Antigravity, Hermes) shipped major architecture changes in the last 90 days.

## Legend
✅ has it, solid/leading · ⚠️ partial, early, or workaround-only · ❌ absent or not found in research · — not applicable to this product's category

## Products covered
Goose (Block/AAIF), Claude Code (Anthropic), OpenCode (SST), Grok Build (xAI), Pi (earendil-works), OpenAI Codex, Antigravity/Gemini CLI (Google), Cursor, Amp (Sourcegraph), Devin (Cognition), Cline, Aider, OpenHands, Warp, Factory (Droids), Replit Agent 3, OpenClaw, Hermes (Nous Research). Plus **GitHub Agent HQ** as the "humans + agents side by side" 2026 buzz release (not a harness itself, but the orchestration layer competitors are converging toward).

---

## THE BIG MATRIX

| Feature | Goose | Claude Code | OpenCode | Grok Build | Pi | Codex | Antigravity | Cursor | Amp | Devin | Cline | Aider | OpenHands | Warp | Factory | Replit Agent 3 | OpenClaw | Hermes |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Plan/read-only mode | ⚠️ | ✅ | ✅ (plan agent) | ✅ (plan-first) | ❌ (minimal core) | ✅ (approval modes) | ✅ | ✅ | ⚠️ | — (autonomous) | ✅ (Plan/Act) | ✅ (architect) | ⚠️ | ✅ | ✅ | ⚠️ | ❌ | ⚠️ |
| Todo/task tracking | ⚠️ | ✅ | ✅ | ⚠️ | ❌ | ✅ | ✅ | ✅ | ✅ | ✅ (native tickets) | ⚠️ | ❌ | ✅ | ⚠️ | ✅ (Missions) | ✅ | ⚠️ | ⚠️ |
| Self-verification loop | ⚠️ | ✅ | ⚠️ | ✅ (Arena Mode) | ❌ | ✅ | ⚠️ | ✅ (browser+test loop) | ✅ | ✅ | ✅ (test loop) | ⚠️ | ✅ | ✅ | ✅ | ✅ (browser self-test) | ❌ | ✅ (outcome eval) |
| Multi-agent / parallel subagents | ⚠️ | ✅ | ✅ | ✅ (8 parallel) | ❌ | ✅ (manager+workers) | ✅ | ✅ (background agents) | ⚠️ | ⚠️ | ⚠️ | ❌ | ✅ (delegation) | ⚠️ | ✅ (Oz) | ✅ (coordinator+droids) | ⚠️ | ❌ | ⚠️ |
| Compaction/context mgmt | ⚠️ | ✅ | ✅ | ⚠️ | ✅ (minimal by design) | ✅ | ⚠️ | ✅ | ✅ (200K working set) | ⚠️ | ⚠️ | ✅ (repo-map) | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| Long-horizon autonomous runs | ⚠️ | ✅ (background tasks) | ⚠️ | ⚠️ | ❌ | ✅ (cloud) | ⚠️ | ✅ (background agents) | ⚠️ | ✅ (multi-day) | ⚠️ | ❌ | ✅ | ⚠️ | ✅ (Oz/webhooks) | ✅ (Missions, hrs/days) | ✅ (200 min) | ⚠️ | ⚠️ |
| Checkpoint/rewind/undo | ⚠️ | ✅ (sessions) | ⚠️ | ⚠️ | ❌ | ⚠️ | ⚠️ | ✅ | ⚠️ | ⚠️ | ⚠️ | ✅ (git undo/redo) | ✅ (history restore) | ⚠️ | ⚠️ | ⚠️ | ❌ | ⚠️ |
| Shell / file ops | ✅ | ✅ | ✅ | ✅ | ✅ (core 4 tools) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Web search/fetch | ✅ (MCP ext) | ✅ | ✅ (MCP) | ✅ (Connectors) | ⚠️ (BYO MCP) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ❌ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ |
| Browser / computer use | ⚠️ (MCP ext) | ⚠️ | ⚠️ | ⚠️ | ❌ | ⚠️ | ✅ | ✅ (visual verify) | ⚠️ | ✅ | ✅ (Computer Use) | ❌ | ✅ (sandbox browser) | ✅ (Full Terminal Use, not browser) | ⚠️ | ✅ | ⚠️ (OpenHands-skill style) | ⚠️ |
| Image/vision input | ⚠️ | ✅ | ⚠️ | ✅ (grok-build-0.1 image+text) | ⚠️ (model-dependent) | ✅ | ✅ (Gemini native multimodal) | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ✅ | ⚠️ | ⚠️ |
| MCP support | ✅ (70+ ext, MCP-native) | ✅ | ✅ | ✅ (BYO-MCP) | ⚠️ (BYO) | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ (Marketplace) | ⚠️ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ⚠️ |
| Custom tool authoring | ✅ | ✅ (skills) | ✅ | ✅ (Grok Skills) | ⚠️ (minimal core, extend via MCP) | ✅ | ✅ (plugins) | ✅ | ⚠️ | ⚠️ | ✅ (SDK, May 2026) | ⚠️ | ✅ (SDK) | ⚠️ | ✅ | ⚠️ | ✅ (AgentSkills, 100+) | ✅ (skill distillation) |
| Local / on-prem models | ✅ (Ollama, 15+ providers) | ⚠️ (Bedrock/Vertex only) | ✅ | ❌ (xAI-only) | ✅ (37+ models/20+ providers) | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ❌ | ✅ (30+ providers) | ✅ (dozens) | ✅ (100+ via SDK) | ⚠️ | ⚠️ | ⚠️ | ✅ (BYOK any) | ✅ |
| Model routing/fallback | ⚠️ | ⚠️ | ✅ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ✅ | ✅ (flagship: multi-model router) | ⚠️ | ⚠️ | ⚠️ (architect/editor split) | ✅ (100+ provider router) | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| Prompt caching | ⚠️ | ✅ | ⚠️ | ⚠️ | ⚠️ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| BYO-key option | ✅ | ✅ (API) | ✅ | ⚠️ (sub-first) | ✅ (BYOK-only) | ✅ | ⚠️ | ✅ | ⚠️ | ❌ (managed only) | ✅ | ✅ | ✅ | ⚠️ | ⚠️ | ⚠️ | ✅ | ✅ |
| Repo/semantic indexing | ⚠️ | ⚠️ (real-time grep/AST, no upfront index) | ⚠️ | ⚠️ | ❌ | ⚠️ | ⚠️ | ✅ (index-first) | ✅ (Sourcegraph code graph) | ✅ | ⚠️ | ✅ (repo-map) | ⚠️ | ⚠️ | ⚠️ | ✅ (Knowledge droid) | ❌ | ⚠️ |
| Multi-file edits | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ⚠️ | ⚠️ |
| Git integration depth | ⚠️ | ✅ | ⚠️ | ⚠️ | ⚠️ | ✅ (worktrees) | ✅ (worktrees) | ✅ | ⚠️ | ✅ | ⚠️ | ✅ (best-in-class: atomic commits, attribution) | ⚠️ | ⚠️ | ✅ | ⚠️ | ❌ | ⚠️ |
| PR authoring/review | ⚠️ | ✅ | ⚠️ | ⚠️ | ❌ | ✅ | ✅ | ✅ | ✅ (agentic review) | ✅ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ✅ (review droid) | ✅ | ❌ | ⚠️ |
| Test running/gates | ✅ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ✅ | ✅ (self-correcting loop) | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⚠️ | ✅ (test droid) | ✅ (autonomous test writing) | ❌ | ⚠️ |
| Project memory files (AGENTS.md-class) | ✅ | ✅ (CLAUDE.md + @AGENTS.md import) | ✅ | ✅ (CLAUDE.md-compatible) | ⚠️ | ✅ (co-author of spec) | ✅ | ✅ | ✅ | ✅ | ✅ (.clinerules) | ✅ | ✅ | ⚠️ | ✅ | ⚠️ | ⚠️ | ⚠️ |
| Persistent cross-session memory | ⚠️ | ⚠️ (memory tool, project-scoped) | ⚠️ | ⚠️ | ❌ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ✅ (enterprise workspaces) | ⚠️ | ❌ | ⚠️ | ⚠️ | ✅ (Knowledge droid) | ⚠️ | ✅ (remembers across convos) | ✅ (persistent project memory) |
| Self-improvement / skill distillation | ⚠️ | ✅ (Self-Improving Agent plugin, evals) | ❌ | ⚠️ | ❌ | ❌ | ❌ | ❌ | ❌ | ⚠️ | ❌ | ❌ | ⚠️ | ❌ | ⚠️ | ⚠️ | ⚠️ | ✅ (flagship: 5-stage learning loop) |
| Evals harness | ⚠️ | ✅ (evals plugin) | ❌ | ❌ | ❌ | ⚠️ | ❌ | ❌ | ⚠️ | ⚠️ | ❌ | ❌ | ✅ (QA instrumentation) | ❌ | ❌ | ⚠️ | ❌ | ⚠️ |
| Local execution | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ⚠️ (VPC-first) | ✅ | ✅ | ✅ (LocalWorkspace) | ✅ | ⚠️ | ❌ (cloud-only) | ✅ | ✅ |
| Container/Docker sandbox | ⚠️ | ✅ | ⚠️ | ⚠️ | ❌ | ✅ | ✅ | ✅ | ⚠️ | ✅ | ⚠️ | ❌ | ✅ (opt-in, was mandatory) | ⚠️ | ✅ | ✅ | ⚠️ | ✅ (5 backends incl. Docker) |
| Remote/cloud sandbox | ⚠️ | ✅ (isolated cloud sandbox) | ⚠️ | ⚠️ | ❌ | ✅ (per-task cloud VM) | ✅ | ✅ (cloud VM + self-hosted) | ⚠️ | ✅ (SaaS or VPC) | ⚠️ | ❌ | ✅ (enterprise) | ✅ (Oz) | ✅ | ✅ | ❌ | ✅ (Modal backend) |
| microVM isolation (Firecracker-class) | ❌ | ⚠️ (bubblewrap/seatbelt, not microVM) | ❌ | ❌ [UNVERIFIED] | ❌ | ⚠️ [UNVERIFIED] | ⚠️ [UNVERIFIED] | ⚠️ [UNVERIFIED] | ❌ | ⚠️ [UNVERIFIED] | ❌ | ❌ | ⚠️ (via 3rd-party e.g. Modal/E2B) | ❌ | ⚠️ [UNVERIFIED] | ⚠️ [UNVERIFIED] | ❌ | ⚠️ (Modal backend = microVM-class) |
| Worktree support | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ❌ | ✅ | ✅ | ✅ (local worktree agents) | ⚠️ | ❌ | ⚠️ | ❌ | ❌ | ⚠️ | ⚠️ | ⚠️ | ❌ | ❌ |
| Air-gapped / no-egress option | ⚠️ (local-first by design) | ⚠️ | ⚠️ | ❌ | ✅ (fully local possible) | ❌ | ❌ | ❌ | ❌ | ⚠️ (VPC) | ⚠️ | ✅ | ⚠️ (self-hosted) | ❌ | ❌ | ❌ | ✅ (local-first) | ✅ (local backend) |
| Fine-grained permission model | ⚠️ | ✅ (permission modes, hooks) | ⚠️ | ⚠️ | ⚠️ (approval-lite) | ✅ (3 approval tiers) | ⚠️ | ✅ | ⚠️ | ⚠️ | ✅ (per-action approval) | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| OS-level sandboxing | ⚠️ | ✅ (bubblewrap/seatbelt) | ⚠️ | ⚠️ | ❌ | ⚠️ | ⚠️ | ⚠️ (CVE-2026-48124, fixed 3.0.0) | ⚠️ | ⚠️ | ⚠️ | ❌ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| Audit logging | ⚠️ | ✅ | ⚠️ | ⚠️ | ❌ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ✅ (enterprise) | ⚠️ | ❌ | ⚠️ | ⚠️ | ✅ | ⚠️ | ❌ | ⚠️ |
| Supply-chain signing | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| SOC2/FedRAMP/HIPAA/ISO cert | ⚠️ [UNVERIFIED per-tier] | ⚠️ (Anthropic org-level; not FedRAMP for CC itself [UNVERIFIED]) | ❌ | ❌ [UNVERIFIED] | — (no vendor) | ⚠️ [UNVERIFIED] | ⚠️ [UNVERIFIED] | ⚠️ (inherits Anthropic SOC2/GDPR infra) | ⚠️ [UNVERIFIED] | ⚠️ [UNVERIFIED] | ❌ | — | ⚠️ (Enterprise self-host claims) | ❌ [UNVERIFIED] | ⚠️ [UNVERIFIED] | ⚠️ [UNVERIFIED] | ❌ | ❌ |
| SSO/RBAC/admin console | ❌ | ⚠️ (via Team/Enterprise plan) | ❌ | ⚠️ | ❌ | ⚠️ | ⚠️ | ✅ (Teams/Enterprise) | ⚠️ | ✅ (enterprise) | ⚠️ | ❌ | ✅ (Enterprise) | ⚠️ | ✅ (enterprise) | ⚠️ | ❌ | ❌ |
| Skills/plugin marketplace | ⚠️ (extension list) | ✅ (large 3rd-party marketplace ecosystem) | ⚠️ | ✅ (Grok Skills, Claude-skill compatible) | ❌ | ⚠️ | ✅ (Antigravity plugins) | ⚠️ | ⚠️ | ❌ | ✅ (MCP Marketplace) | ❌ | ❌ | ⚠️ | ❌ | ❌ | ✅ (100+ AgentSkills) | ⚠️ |
| SDK for building on top | ✅ (framework-first) | ✅ (Claude Agent SDK) | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ✅ (May 2026) | ❌ | ✅ (Software Agent SDK) | ⚠️ | ⚠️ | ⚠️ | ⚠️ | ⚠️ |
| CLI surface | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (Antigravity CLI) | ⚠️ (IDE-first) | ✅ | ❌ | ✅ (preview) | ✅ | ⚠️ | ✅ | ✅ | ❌ | ✅ | ✅ |
| Desktop app | ✅ | ⚠️ | ✅ | ⚠️ | ❌ | ✅ | ✅ | ✅ (full IDE) | ❌ | ✅ | ❌ | ❌ | ⚠️ | ⚠️ | ✅ | ❌ (browser-based) | ⚠️ | ✅ (preview, June 2026) |
| IDE integration | ⚠️ | ✅ (VS Code/JetBrains ext) | ✅ (VS Code ext) | ⚠️ | ⚠️ | ✅ | ✅ (own IDE) | ✅ (is the IDE) | ✅ (VS Code ext) | ⚠️ | ✅ (VS Code/JetBrains/Cursor/Windsurf/Zed/Neovim) | ⚠️ | ⚠️ | ❌ | ⚠️ | ✅ (web IDE) | ❌ | ❌ |
| Chat-platform surfaces | ⚠️ | ❌ | ❌ | ⚠️ | ❌ | ❌ | ⚠️ | ❌ | ❌ | ⚠️ | ❌ | ❌ | ❌ | ❌ | ⚠️ (Slack webhook) | ❌ | ✅ (20+ platforms) | ✅ (Telegram/Discord/Slack/WhatsApp/Signal/Email) |
| Open source | ✅ (Apache 2.0) | ❌ (closed, open Agent SDK) | ✅ (open) | ❌ | ✅ (MIT) | ✅ (CLI open) | ❌ | ❌ | ❌ | ❌ | ✅ (Apache 2.0) | ✅ | ✅ (MIT) | ✅ (MIT/AGPLv3 dual) | ❌ | ❌ | ✅ | ✅ (mostly open) |

---

## Per-dimension narrative

### 1. Core loop
Claude Code, Cursor, Factory, and Warp lead on the full loop (plan → execute → self-verify → retry). **Grok Build's Arena Mode** — running up to 8 parallel agents on the same problem and having a judge/you pick the winner — is a genuinely distinct bet on parallelism-over-depth rather than deeper single-agent reasoning [buildfastwithai.com](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026), [developersdigest.tech](https://www.developersdigest.tech/blog/grok-build-developer-guide-2026). **Factory's Missions** and **Devin's** multi-day autonomous ticket execution are the clearest "long-horizon" plays [rywalker.com](https://rywalker.com/research/factory-ai), [eesel.ai](https://www.eesel.ai/blog/cognition-ai). **Replit Agent 3** pushes single-session autonomy furthest for a consumer-facing product (200 minutes, 10x Agent 2) [blog.replit.com](https://blog.replit.com/introducing-agent-3-our-most-autonomous-agent-yet). Checkpointing is real but shallow everywhere — Claude Code sessions and Aider's git-native undo/redo are the most usable, but none of these tools truly rewind side effects (API calls, DB writes) [claude docs via search].

### 2. Tools
Universal baseline (file ops, shell, multi-file edit) is table stakes — everyone has it. The differentiator is **browser/computer use**: Cline's Computer Use, Cursor's visual verification (opens localhost, clicks through UI), Devin, and OpenHands sandbox-browser access are the leaders; Aider and Pi deliberately have none (by design — Pi ships only 4 tools) [llmreference.com](https://www.llmreference.com/agents/pi). MCP is now near-universal; the frontier there has moved to *tool-result caching and code-mode MCP adapters* (OpenCode shipped one in 2026) [pulsemcp.com](https://www.pulsemcp.com/clients/sst-opencode).

### 3. Model layer
Model-layer differentiation is thinning as frontier models converge — several 2026 comparisons say the harness now matters more than the model [firecrawl.dev](https://www.firecrawl.dev/blog/best-ai-coding-agents). Goose, Pi, Aider, Cline, and OpenHands are the true multi-provider/local-model leaders (15–100+ providers, Ollama support). **Amp's multi-model router** — routing subtasks to the best-suited model rather than running one model end to end — is the most technically distinctive routing implementation found [skywork.ai](https://skywork.ai/skypage/en/ultimate-guide-amp-ai-coding/2044314087912464384). Grok Build and Antigravity are single-vendor by design. Pricing has converged hard: most serious individual tiers sit at ~$20/mo, power tiers at $60–200/mo [nxcode.io](https://www.nxcode.io/resources/news/ai-coding-tools-pricing-comparison-2026), [ai-cost-estimator.com](https://ai-cost-estimator.com/blog/cursor-vs-windsurf-vs-claude-code-ai-ide-pricing-june-2026).

### 4. Code capabilities
Three competing philosophies for repo understanding: **index-first** (Cursor, Amp/Sourcegraph code graph), **real-time-tool-based** (Claude Code — no upfront index, uses grep/AST/glob live), and the emerging **knowledge-graph-via-MCP** approach (CodeGraph: 47.4k stars in 5 months; reports ~10x token reduction and 2.1x fewer tool calls vs RAG) [anthonywest.co.uk](https://anthonywest.co.uk/research/code-intelligence-indexing-2026-openai), [medium.com/kd-agentic](https://medium.com/kd-agentic/codegraph-the-open-source-knowledge-graph-that-makes-ai-coding-tools-dramatically-cheaper-190f8b89f8a7), [sourcegraph.com](https://sourcegraph.com/blog/agentic-coding). Aider remains the git-integration benchmark — atomic per-turn commits, undo/redo, AI-authorship attribution — widely called "most complete and trustworthy" in 2026 reviews [deployhq.com](https://www.deployhq.com/guides/aider). Factory's specialized droid-per-function (code/review/docs/test) is the clearest division-of-labor architecture [digitalapplied.com](https://www.digitalapplied.com/blog/factory-ai-multi-agent-coding-platform-review).

### 5. Memory & knowledge
This is the most fragmented dimension. AGENTS.md is now the de facto cross-tool memory-file standard — 60,000+ repos, 28+ tools with native support, formalized Aug 2025 by OpenAI/Google/Cursor/Factory/Sourcegraph, now stewarded by the Linux Foundation's Agentic AI Foundation; **Claude Code is the named holdout**, still requiring a CLAUDE.md-to-AGENTS.md symlink bridge [codersera.com](https://codersera.com/blog/agents-md-complete-guide-2026/), [iuriio.com](https://www.iuriio.com/blog/posts/2026/05/agents-md-field-guide-2026). True persistent, cross-session, self-improving memory is rare: **Hermes is the standout** — a 5-stage loop (execute → evaluate → extract reusable skill → refine → retrieve) that distills solved problems into named, reusable skills [techjacksolutions.com](https://techjacksolutions.com/ai-tools/hermes/hermes-breakdown/), [aiengineerinsights.com](https://aiengineerinsights.com/blog/hermes-agent-nous-research-guide/). Claude Code's Self-Improving Agent plugin (auto-memory → promote to CLAUDE.md/rules → extract skills) is the closest analog in the Claude ecosystem, but it is a community plugin, not core [scriptbyai.com](https://www.scriptbyai.com/claude-code-resource-list/). Evals-as-a-feature exist in Claude Code (evals plugin: with/without-skill comparison, pass-rate/token aggregation) and OpenHands (QA instrumentation) but nowhere else in a first-class way.

### 6. Execution environments
Sandbox infrastructure has become its own market layer, decoupled from the harnesses themselves: **E2B** (Firecracker microVMs, hardware-kernel isolation, 5–30ms snapshot-restore) and **Daytona** (pivoted fully to AI code execution, ~90ms cold start) are the dominant third-party substrates that Devin, OpenHands, and others build on [northflank.com](https://northflank.com/blog/daytona-vs-e2b-ai-code-execution-sandboxes), [spheron.network](https://www.spheron.network/blog/ai-agent-code-execution-sandbox-e2b-daytona-firecracker/). Cursor's three-tier background-agent model (Cursor-hosted cloud VM / self-hosted cloud / local worktree) is the most flexible deployment matrix found for a single product [ameany.io](https://ameany.io/cursor-background-agents/). OpenHands' V1 SDK move from *mandatory* Docker to *opt-in* sandboxing (LocalWorkspace default) is notable — it's trading security-by-default for lower friction [arxiv.org/2511.03690](https://arxiv.org/html/2511.03690v1). None of the harnesses themselves ship native Firecracker/microVM isolation — that capability is bought in from E2B/Daytona/Modal, not built.

### 7. Security
2026 has been a rough year for sandbox trust: the Claude Code source leak (March 31, 512K lines of TypeScript via npm) led to a discovered deny-rule bypass in `bashPermissions.ts`, and Cursor shipped CVE-2026-48124 (a workspace-controlled `.claude` hook config escaping to unsandboxed execution, fixed in 3.0.0) [thenewstack.io](https://thenewstack.io/agentjacking-sentry-mcp-attack/], [bleepingcomputer.com](https://www.bleepingcomputer.com/news/security/cursor-codex-gemini-cli-antigravity-hit-by-sandbox-escapes/). A 78-study academic survey rated Claude Code's overall vulnerability "Low" vs. Cursor "Critical" and Copilot "High," attributing the gap to Claude Code's mandatory tool confirmation plus OS-level sandboxing (Linux bubblewrap / macOS seatbelt) [truefoundry.com](https://www.truefoundry.com/blog/claude-code-prompt-injection). Formal compliance certification (SOC2/FedRAMP/HIPAA/ISO) is **largely unverifiable per-product from public sources** at the harness level — most claims found were "inherits provider infrastructure" (e.g., Cursor via Anthropic's SOC2/GDPR posture) rather than the harness itself being certified [augmentcode.com](https://www.augmentcode.com/guides/7-soc-2-ready-ai-coding-tools-for-enterprise-security). No FedRAMP authorization for any of these 18 products was found — this is a real, still-open gap in the market, consistent with Arc's positioning thesis.

### 8. Collaboration & enterprise
Devin, Factory, Cursor, and OpenHands Enterprise have real SSO/RBAC/admin-console stacks. Devin's "AI Productivity Guarantee" (funds usage up to $10M if it underdelivers) is a unique enterprise commercial structure [research.contrary.com](https://research.contrary.com/company/cognition). Factory's $150M Series C at $1.5B (Khosla, Sequoia, Blackstone, Insight; customers include Nvidia, Adobe, Bayer, EY, MongoDB, Zapier) signals the most enterprise commercial traction of the pure-play agent startups [tech-insider.org](https://tech-insider.org/factory-ai-150-million-series-c-khosla-coding-droids-2026/). **GitHub Agent HQ** (launched Feb 4, 2026) is the platform-level answer to "humans and agents side by side": a vendor-neutral orchestration layer running Claude, Codex, and Copilot interchangeably inside GitHub/VS Code/Mobile, using a "Blackboard" shared-state architecture instead of direct agent-to-agent chatter, with a Human-in-the-Loop arbitration step when agents' plans conflict [bittalks.org](https://bittalks.org/blog/github-agent-hq-2026/), [eficode.com](https://www.eficode.com/blog/why-github-agent-hq-matters-for-engineering-teams-in-2026).

### 9. Extensibility
Claude Code has the largest third-party plugin/skill ecosystem found (multiple marketplaces cataloging hundreds of plugins and thousands of skills) [github.com/jeremylongshore](https://github.com/jeremylongshore/claude-code-plugins-plus-skills). Grok Skills were shipped explicitly compatible with Claude Code skills/plugins/CLAUDE.md — a direct interoperability bet by xAI [codersera.com](https://codersera.com/blog/xai-grok-build-skills-connectors-guide-2026/). OpenClaw's 100+ "AgentSkills" and 20+ chat-platform integrations make it the most extensible on the *personal-automation* axis, even though it isn't primarily a coding tool [digitalocean.com](https://www.digitalocean.com/resources/articles/what-is-openclaw), [kdnuggets.com](https://www.kdnuggets.com/openclaw-explained-the-free-ai-agent-tool-going-viral-already-in-2026). Cline shipping an embeddable SDK (May 2026) so others can build custom agents on its runtime is a notable extensibility-as-a-platform move [buildfastwithai.com](https://www.buildfastwithai.com/ai-tools/cline).

### 10. Surfaces
Warp's "universal agent" move — running Claude Code, Codex, Gemini CLI, and OpenCode *inside* Warp with vertical tabs and status indicators — is the clearest meta-surface bet: don't build the best agent, build the best place to run everyone else's agent [deployhq.com](https://www.deployhq.com/guides/warp). Vercel's AI SDK v7 HarnessAgent API (June 2026) is doing the same thing at the SDK layer for Claude Code, Codex, and Pi [llmreference.com](https://www.llmreference.com/agents/pi). This "meta-harness" pattern — an abstraction layer that runs multiple vendor agents interchangeably — is arguably more consequential than any single product's surface.

### 11. Pricing & packaging / OSS status
Genuinely open source (permissive license, real community): Goose (Apache 2.0, now Linux Foundation AAIF-governed), OpenCode, Cline (Apache 2.0), Aider, OpenHands (MIT), Warp (dual MIT/AGPLv3, opened 2026), Pi (MIT), OpenClaw, and Hermes (mostly open). Closed-core-with-open-CLI: Claude Code (Agent SDK is open, core product is not), OpenAI Codex (CLI is open-source). Fully closed: Grok Build, Antigravity, Cursor, Amp, Devin, Factory, Replit Agent. Pricing has converged to three tiers across nearly the whole market: free/entry (~$0–15), prosumer (~$20/mo, now the modal price point), and power-user (~$60–200/mo) [nxcode.io](https://www.nxcode.io/resources/news/ai-coding-tools-pricing-comparison-2026).

---

## FEATURE FRONTIER — what only 1–2 products have today

These look like where the puck is going, because they solve a problem every other product still has:

1. **Skill self-distillation from solved tasks** (Hermes) — turning a solved problem into a reusable named skill automatically, not via a manual `/compound`-style command. Nobody else in the coding-agent space does this natively; Claude Code's Self-Improving Agent plugin is the nearest analog but is a community add-on.
2. **Arena Mode / N-way competing solutions** (Grok Build) — running the same task through multiple parallel agents and picking a winner, rather than one agent self-correcting. A fundamentally different bet on how you buy reliability (breadth vs. depth).
3. **Business-outcome-level autonomy over hours/days** (Factory Missions, and to a lesser extent Devin) — natural-language *outcomes*, not tickets, as the unit of delegation.
4. **Meta-harness / harness-agnostic control planes** (Warp's universal-agent hosting, Vercel's HarnessAgent API, GitHub Agent HQ's Blackboard) — treating Claude Code/Codex/Gemini CLI/Pi as interchangeable backends behind one UI. This is the single clearest 2026 trend line and almost nobody outside these three had it a year ago.
5. **Knowledge-graph-over-MCP repo understanding** (CodeGraph, GitNexus) as a *pluggable* layer any harness can attach, rather than a proprietary index (Cursor/Amp) — reported ~10x token savings.
6. **Live browser self-testing as a first-class loop, not a tool call** (Replit Agent 3, Cursor) — the agent visually verifies its own UI work before returning control.
7. **Federated open-governance of core infra** (MCP + Goose + AGENTS.md all donated into the Linux Foundation's Agentic AI Foundation by direct competitors) — de-risking lock-in at the standards layer while competing everywhere else.
8. **Firecracker-class microVM isolation bought in as infrastructure, not built** (E2B/Daytona under Devin, OpenHands-enterprise, others) — the isolation guarantee is becoming a commodity layer rather than a differentiator, which will make it table stakes fast.

---

## THE 20 FEATURES A NEW ENTRANT MUST SHIP, RANKED BY CROSS-PRODUCT FREQUENCY

Ranked by how many of the 18 products already have it solidly (✅) — i.e., how non-negotiable it already is — with the frontier items called out where a new entrant could still differentiate.

1. Shell/file multi-file edit ops — universal, zero-differentiation floor.
2. Git integration (commits, diff review) — universal floor; Aider shows the ceiling.
3. MCP support — now near-universal; ship it on day one.
4. Test running / build-lint gates — universal floor.
5. Project memory file (AGENTS.md or equivalent) — universal; support the standard, don't invent your own.
6. Multi-file/multi-step task execution — universal floor.
7. Plan/read-only mode before acting — majority have it; missing it reads as reckless in 2026.
8. Fine-grained per-action permission/approval model — majority; the security bar keeps rising after the March/April sandbox-escape wave.
9. Web search/fetch tool — majority.
10. BYO-key / BYO-model option — majority; pure-subscription-only is now a minority position.
11. Multi-provider / model-agnostic core — majority among the open players; increasingly expected even from closed ones.
12. Subagent / parallel-task delegation — majority and rising fast.
13. Custom tool/skill authoring — majority; skills/plugins are the extensibility norm now.
14. Cloud/remote sandbox execution option — majority, especially among funded entrants.
15. PR authoring — majority among anything git-native.
16. CLI surface — majority; even IDE-first products (Cursor) face pressure here.
17. OS-level or container sandboxing by default — rising fast post sandbox-escape CVEs; will be a hard requirement within a year.
18. Self-verification/retry loop (tests, browser checks) — rising fast; table stakes for anything claiming "autonomous."
19. Long-horizon autonomous run capability (hours, not minutes) — becoming a competitive requirement (Replit, Factory, Devin all racing here).
20. Persistent, self-improving cross-session memory — still rare (Hermes leads) but named by multiple vendors as the next battleground; a new entrant that ships this well differentiates rather than just keeps pace.

**What's conspicuously missing across the board, and would be a genuine wedge:** verified compliance certification (SOC2/FedRAMP/HIPAA) at the harness level, and native (not bought-in) microVM isolation. No product in this set was confirmed to have either — that's the gap Arc's federal-first thesis is built to occupy.

---

## Research gaps / honesty notes
- Supply-chain signing and audit-logging depth were not clearly documented for any product beyond Claude Code and Devin; most matrix cells there are ⚠️ because I could not confirm absence vs. simply undocumented.
- Formal compliance certifications (row 33) are the weakest-sourced row in the matrix — treat every non-❌ cell there as provisional; vendor compliance pages were not directly fetched for all 18 products given time constraints.
- OpenClaw is a personal-automation agent, not a coding harness first — several coding-specific rows (repo indexing, PR authoring, test gates) are legitimately ❌/⚠️ for it rather than research gaps.
- Grok Build, Antigravity, and Hermes all had major architecture changes in the 60–90 days before this research (Grok Build beta expansion, Gemini CLI→Antigravity CLI transition, Hermes v0.18.0) — some cells may already be stale by the time this is read.
