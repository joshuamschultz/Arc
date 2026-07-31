# Quick Deploy / Time-to-Value Research — Agent Platforms (July 2026)

## (a) Time-to-first-value table

| Product | Install command | # steps to first output | Prerequisites | Est. minutes |
|---|---|---|---|---|
| **Goose (Block)** | `curl -fsSL https://github.com/aaif-goose/goose/releases/download/stable/download_cli.sh \| bash` (CLI) or native desktop installer (.dmg/.exe/.deb/.rpm/Flatpak) | ~3: install → launch → pick provider/paste API key | API key for one LLM provider; low-risk test dir recommended | 3–5 |
| **Claude Code** | `npm install -g @anthropic-ai/claude-code` (or brew/native installer) | ~2: install → `claude` login (OAuth or API key) | Node.js, Anthropic account | <2 |
| **OpenHands** | Historically `docker run -it ... openhands` one-liner; 2026 maintainers now recommend **uv-based CLI install**, not Docker | ~4+: install uv → clone/install → configure API key → run; Docker path adds image pull + volume mounts + MCP server needs `uv` anyway | Python 3.12+, uv, (optionally Docker) | 10–15 (Docker path slower than Claude Code's ~2 min) |
| **Coolify** | One-line VPS install script (root/sudo) | ~2: run install script → open dashboard, auto SSL/proxy configured | Ubuntu 22.04+, 2GB+ RAM, root/sudo | ~5 (30 sec per app after that) |
| **Ollama + Open WebUI** | `docker run -d -p 3000:8080 ... ghcr.io/open-webui/open-webui:main` + separate Ollama install | ~3: install Ollama → run Open WebUI container with `--add-host=host.docker.internal:host-gateway` → pick model (auto-detected) | Docker, local disk for model weights | 5–10 (+ model download time) |
| **n8n** | Self-host via Docker/npm, or n8n Cloud signup | ~2–3: deploy → open editor → import a template | Docker or Node, or zero for Cloud | 2–10 |
| **OpenCode** | `curl -fsSL https://opencode.ai/install \| bash` | ~4: install → verify → connect provider (not Claude, blocked since Jan 2026) → run | API key for GPT-5.4/Gemini/etc. | 2–3 |
| **Gemini CLI** | `npx @google/gemini-cli` (no install) or `npm i -g @google/gemini-cli` / `brew install gemini-cli` | ~2: run → "Sign in with Google" browser flow | Node 18+, Google account | 1–2 |
| **OpenAI Codex CLI** | `npm i -g @openai/codex` / `brew install codex` / `winget install OpenAI.Codex` | ~3: install → `codex login` (OAuth browser) → run | Node 22+ (or prebuilt Rust binary, zero-dep); ChatGPT Plus/Pro/Business plan | 1–2 |
| **AWS Bedrock AgentCore** | `npm install -g @aws/agentcore-cli` → `agentcore init` → `agentcore dev` → `agentcore deploy` | 5: install Node/Python/AWS CLI/CDK → install CLI → `cdk bootstrap` → init project → dev/deploy | AWS account, IAM perms, CDK bootstrap | ~3 min for first Runtime provision, but ~20-30 min all-in with AWS setup |
| **Cursor** | Native installer (.exe/.dmg) with setup wizard | ~3: install → onboarding wizard (VS Code import) → Agent Mode picks a model | Account/API key, 8GB+ RAM recommended | ~10 (wizard-driven) |
| **Devin** | Web app signup → GitHub OAuth → select repos | ~3: signup → connect GitHub → pick repo (Devin Wiki auto-indexes) | GitHub account, Devin subscription | Indexing time [UNVERIFIED — no published benchmark found] |

## (b) Zero-config / auto-detect matrix

| Product | Auto-detected | Preconfigured defaults |
|---|---|---|
| Claude Code | Repo language/tooling via CLAUDE.md conventions, MCP servers pre-listed in official marketplace | Anthropic marketplace ships pre-added; `/doctor`/`/checkup` auto-audits config |
| Goose | N/A for repo type explicitly, but extension directory (70+) is auto-browsable | First-run wizard walks provider + key only |
| Open WebUI | Ollama models auto-populate the model picker once host networking is bridged | Multi-user auth, history sidebar on by default |
| Coolify | Buildpacks (Nixpacks) auto-detect language (Node/PHP/Python/Ruby/Go/static) from repo | SSL, reverse proxy, one-click DBs preconfigured |
| n8n | Node type suggestions during workflow build | Templates pre-import triggers/credentials scaffolding |
| Bedrock AgentCore | Framework detection for code-based agents (Strands/LangGraph/Google ADK/OpenAI Agents SDK) | Managed-harness mode needs only a config file — no orchestration code |
| Cursor | Existing VS Code extensions/themes/keybindings/settings imported on first run | Agent Mode default workflow, background agents in cloud sandboxes |
| Devin | Full codebase auto-indexed into "Devin Wiki" (architecture docs) and "Devin Search" (NL Q&A) on repo connect | N/A |

## (c) Templates / recipes / marketplace ecosystems

| Product | Ecosystem | Count | Discovery/install |
|---|---|---|---|
| **n8n** | Community workflow templates | Official library ~9,100–10,900+; 18,000+ across all sources incl. GitHub collections (280+) and paid marketplaces (7,000+, 850+) | Browse/filter/search at n8n.io/workflows; one-click import into editor |
| **Goose** | Recipes (YAML: instructions+extensions+params+retry) + Extensions (MCP) | 70+ officially supported extensions; open "Skills Marketplace" for community recipes | `goose recipe` CLI / desktop browse; share via YAML file |
| **Claude Code** | Plugin marketplaces (skills, agents, commands, hooks, MCP bundles) | Anthropic official marketplace pre-added; unlimited third-party via `/plugin marketplace add <repo>`; npm-distributable | `/plugin marketplace add`, then `/plugin install`; supports GitHub/npm/GitLab/local sources, version pinning, auto-update |
| **Copilot Studio** | Agent templates (Website Q&A, Citizen Services, IT Helpdesk, etc.) + M365 CAPE-team template repo | Exact gallery count [UNVERIFIED] — no published total found | "Start with an agent template" in Agents page; also github.com/microsoft/m365-agent-templates |
| **Bedrock AgentCore** | Sample agents repo | awslabs/agentcore-samples (count not enumerated) [UNVERIFIED] | GitHub samples repo, `agentcore init` scaffolds |
| **OpenHands** | Microagents / skill repos | [UNVERIFIED — no count found] | repo-based |

Takeaway: **n8n has by far the largest and most discoverable template ecosystem** (order of magnitude larger than any agent-coding tool). **Claude Code's differentiator is the marketplace *mechanism*** (versioned, multi-backend, auto-update) more than raw count. **Goose's recipes are the closest analog to "quick deploy blueprints" for a coding agent** — a shareable, parameterized YAML bundle.

## (d) Fleet deployment comparison

| Product | 1-agent → N-agents path | Tooling |
|---|---|---|
| Claude Code | Community-built patterns, not first-party: Kube Foundry (operator pattern, Helm), chrisbattarbee/claude-code-helm chart, HF blog walkthrough for spinning up many agents on K8s. Hosting the Agent SDK docs note each agent is a long-lived stateful process (shell + workdir + session files), so allocation/persistence must be designed per-tenant. | Kubernetes + Helm (third-party), Agent SDK for programmatic hosting |
| Bedrock AgentCore | First-party, most mature "fleet" story: managed Runtime scales per-invocation, Gateway for tool fan-out, built-in evals, CDK/CLI-driven | `agentcore` CLI + CDK, AWS-native scale-to-zero/scale-out |
| Coolify | Single-VPS PaaS model; multi-server support exists but framed as "your own Heroku," not an agent-specific fleet | Docker + reverse proxy per node |
| n8n | Horizontal scaling via queue mode (Redis + workers), n8n Cloud tiers | Docker Compose / Helm chart (community + official) |
| OpenHands | No first-party fleet tooling surfaced in search; Docker-per-session is the isolation unit | [UNVERIFIED beyond single-instance Docker] |
| Goose | No first-party multi-agent fleet/control-plane found; recipes + scheduling exist per-machine | [UNVERIFIED] |

**Nobody in the coding-agent segment has a first-party, batteries-included fleet control plane.** AWS AgentCore is the most fleet-mature (because it's cloud infra, not a dev tool). Everyone else's "fleet" story is bolted on by the community (Helm charts, K8s operators) months after the single-agent product shipped. **This is Arc's clearest white space**: ship the operator/control-plane as first-party from day one, not as a community afterthought.

## (e) Onboarding UX patterns worth stealing

1. **Provider-pick-and-paste-key, nothing else** (Goose desktop first-run) — one screen, done. No config file editing required for the common case.
2. **`/doctor` → `/checkup` evolution** (Claude Code, v2.1.205 July 2026) — started as pass/fail health check (API connectivity, MCP health, hook syntax, settings.json, context usage), evolved into an *opinionated* cleanup advisor that proposes concrete fixes (unused skills/MCPs/plugins, stale CLAUDE.md, permission hygiene) rather than just reporting red/green. Arc should ship an `arc doctor` that both diagnoses AND proposes fixes.
3. **Zero-install trial via npx** (Gemini CLI) — `npx @google/gemini-cli` runs with no persistent install; lowers the "am I committing to this" barrier for first trial.
4. **Auto-import existing environment** (Cursor) — onboarding wizard offers to import VS Code extensions/themes/keybindings/settings, converting a competitor's setup into instant familiarity rather than a blank slate.
5. **Repo auto-indexing into human-readable docs** (Devin Wiki/Search) — connecting a repo immediately produces architecture docs and an NL search interface, giving visible value before the agent does any task work.
4. **Buildpack auto-detection** (Coolify/Nixpacks) — zero Dockerfile needed for common stacks; language detected from repo contents.
6. **Managed-harness vs. code-based agent choice at init** (Bedrock AgentCore) — `agentcore init` lets a user choose "just declare it in config, we run the loop" or "bring your own framework," serving both quick-deploy and power users from one entry point.
7. **Recipe as the shareable unit of value** (Goose) — a YAML file bundling instructions + required extensions + params + retry logic is small, diffable, versionable, and immediately runnable — a good template for Arc's "blueprint" concept.
8. **Plugin marketplace with version pinning + auto-update + multi-backend** (Claude Code) — treats extensibility as a distribution problem (like a package manager), not just a discovery problem.

## (f) Friction / failure list

- **OpenHands**: Docker dependency is its single biggest friction point — image pull, volume mounts, API key config take "meaningfully longer" than Claude Code's ~2-minute install; and even the Docker path still needs `uv` installed on the host for default MCP servers, defeating the isolation promise. Org rename (All-Hands-AI → OpenHands, Oct 2025) left old install commands pointing at redirecting/broken image paths. V0 deprecated April 2026 mid-transition — a moving-target install target compounds the friction. [lowcode.agency, qwe.edu.pl, glukhov.org]
- **OpenCode**: Anthropic blocked OpenCode from using Claude models (Jan 2026), forcing a provider switch immediately after install — a "works until it doesn't" surprise for users who assumed universal model support. [medium.com/@rosgluk]
- **Cursor**: Windows setup wizard forces a System-vs-User install-mode decision most users can't evaluate; full setup ~10 minutes, AI features need 8GB+ RAM as an unstated soft requirement. [daily.dev]
- **Bedrock AgentCore**: Heaviest prerequisite chain of any product surveyed — Node 20+, Python 3.10+, AWS CLI, CDK, IAM permissions, CDK bootstrap — before `agentcore init` even runs. Enterprise-grade but not "quick" without AWS familiarity.
- **Copilot Studio**: template gallery has no published/discoverable total count — poor transparency for evaluating ecosystem depth versus n8n or Claude Code marketplaces. [UNVERIFIED]
- **Devin**: no published repo-indexing/time-to-first-task benchmark exists publicly; VPC/self-hosted deployment explicitly trades longer setup time for data-boundary control — a real tradeoff Arc should message explicitly rather than hide. [deployhq.com]

## (g) The deploy experience Arc must beat — concrete target spec

Based on the above, state-of-the-art in 2026 is fragmented: Claude Code wins raw install speed (<2 min, zero deps) and marketplace mechanics; Goose wins "one screen to a working agent" simplicity and recipe portability; Coolify/AWS AgentCore win zero-to-running-infra automation (SSL, buildpacks, CDK); nobody wins fleet-scale first-party tooling. Arc's target, to beat all of them simultaneously:

1. **≤2 minutes, one command, zero required config**: `docker run arc` (already the current path) must reach an interactive first agent turn — not just a running container — in under 2 minutes, matching Claude Code's benchmark, beating OpenHands' Docker tax.
2. **One-screen provider setup**: first run prompts for exactly one thing (LLM provider + key, or "use local model") — Goose's pattern — never a config file edit for the default path.
3. **`arc doctor` from day one**, opinionated like Claude Code's `/checkup`: diagnoses AND proposes/executes fixes (stale skills, missing signatures, policy gaps), not just red/green.
4. **A recipe/blueprint format** (Goose-style YAML: goal + tools + extensions + params, git-diffable) as the shareable unit for "quick-deploy a working agent for X" — ship 15-20 blueprints out of the box (more curated than Copilot Studio's handful, more focused than n8n's 10k-workflow flood).
5. **First-party fleet control plane at launch**, not a 6-months-later community Helm chart: `arc fleet up --agents 20` should be as native as `docker run arc` is for one agent — this is the gap literally nobody (Claude Code, Goose, OpenHands, Cursor) has closed, and where AWS AgentCore only wins because it's cloud infrastructure, not a portable dev tool.
6. **Auto-indexing on connect** (Devin-style): pointing Arc at a repo/environment should immediately produce a readable "here's what I found, here's what I can do" artifact before any task is run — converts idle setup time into visible value.
7. **Explicit security/deployment tradeoff messaging** (unlike Devin's silent VPC-vs-SaaS gap): Arc's federal/enterprise tiering should state plainly what's slower/stricter at higher tiers, since security is the differentiator, not a hidden cost.

## Sources

- [Goose Quickstart](https://goose-docs.ai/docs/quickstart/), [Goose AI Agent Setup](https://knightli.com/en/2026/05/08/goose-open-source-ai-agent-desktop-cli-api/), [Goose Extensions](https://goose-docs.ai/extensions/), [Goose Recipes](https://block.github.io/goose/docs/guides/recipes/), [Goose quickstart blog](https://blog.agentailor.com/posts/goose-open-source-agent-quickstart), [DeepWiki Recipes](https://deepwiki.com/aaif-goose/goose/4-recipes-and-scheduling)
- [Claude Code plugin marketplaces docs](https://code.claude.com/docs/en/plugin-marketplaces), [Plugin Marketplace deep dive](https://ice-ice-bear.github.io/posts/2026-04-03-claude-code-plugin-marketplace/), [/doctor to /checkup](https://smartscope.blog/en/blog/claude-code-checkup-doctor/), [/doctor guide](https://blog.vincentqiao.com/en/posts/claude-code-doctor/)
- [OpenHands install guide](https://www.qwe.edu.pl/ai-tools/openhands-install-guide/), [OpenHands local setup docs](https://docs.openhands.dev/openhands/usage/run-openhands/local-setup), [Claude Code vs OpenHands](https://www.lowcode.agency/blog/claude-code-vs-openhands)
- [Coolify installation docs](https://coolify.io/docs/get-started/installation), [Coolify self-host guide](https://use-apify.com/blog/self-host-coolify-vps)
- [Open WebUI Docker guide](https://shantun.medium.com/open-webui-installation-features-errors-complete-beginner-guide-2026-d814b7a84af1), [Open WebUI setup 2026](https://www.freetechlearner.com/blog/self-hosting/openwebui-setup-guide)
- [n8n workflows library](https://n8n.io/workflows/), [awesome-n8n-templates](https://github.com/enescingoz/awesome-n8n-templates), [n8n templates docs](https://docs.n8n.io/workflows/templates/)
- [Cursor first-time setup](https://daily.dev/blog/setup-cursor-first-time/), [Cursor Agent Mode guide](https://tinyfirm.dev/blog/complete-guide-cursor-agent-mode)
- [OpenCode quickstart](https://medium.com/@rosgluk/opencode-quickstart-install-configure-and-use-the-terminal-ai-coding-agent-a4ac45b1fbad), [OpenCode GitHub](https://github.com/opencode-ai/opencode)
- [Gemini CLI setup guide](https://kissapi.ai/blog/gemini-cli-setup-guide-2026.html), [Gemini CLI installation docs](https://geminicli.com/docs/get-started/installation/)
- [Codex CLI guide 2026](https://serenitiesai.com/articles/openai-codex-cli-guide-2026), [Codex CLI setup](https://blakecrosley.com/guides/codex)
- [AWS Bedrock AgentCore Runtime Quickstart](https://aws.github.io/bedrock-agentcore-starter-toolkit/user-guide/runtime/quickstart.html), [Get started with AgentCore CLI](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/agentcore-get-started-cli.html), [AgentCore announcement](https://aws.amazon.com/blogs/aws/introducing-amazon-bedrock-agentcore-securely-deploy-and-operate-ai-agents-at-any-scale)
- [Copilot Studio template fundamentals](https://learn.microsoft.com/en-us/microsoft-copilot-studio/template-fundamentals), [M365 agent templates](https://microsoft.github.io/m365-agent-templates/)
- [Devin AI review 2026](https://www.buildfastwithai.com/ai-tools/devin), [Devin AI guide/deployment](https://www.deployhq.com/guides/devin)
- [Claude Code on Kubernetes (HF blog)](https://huggingface.co/blog/mclenhard/how-run-claude-code-on-kubernetes), [Agent SDK hosting docs](https://code.claude.com/docs/en/agent-sdk/hosting), [Claude Code on K8s (Metoro)](https://metoro.io/blog/claude-code-kubernetes)
