# Agent Creation, Presentation & Control — 2026 Landscape Survey

Research date: 2026-07-25. Primary benchmark: Block Goose.

## (a) Creation-format comparison — real examples

**1. Goose (Block) — YAML "Recipe"** — the richest single-file agent+workflow format surveyed.
```yaml
title: Research Assistant
instructions: |
  You are a research assistant that helps gather and synthesize information.
  Use web search and file reading tools to find relevant data. Always cite sources.
extensions:
  - type: builtin
    name: developer
  - type: stdio
    name: brave-search
    cmd: npx
    args: ["-y", "@modelcontextprotocol/server-brave-search"]
settings:
  goose_model: claude-sonnet-4-20250514
  max_turns: 30
  temperature: 0.2
sub_recipes:
  - name: "find_files"
    path: "./sub-recipes/file-finder.yaml"
  - name: "analyze_code"
    path: "./sub-recipes/code-analyzer.yaml"
    sequential_when_repeated: true
```
Recipes are shareable, runnable-as-CLI-arg, schedulable, and can spawn sub-recipes as subagents (Lead/Worker pattern); subagents inherit all parent extensions by default, restrictable for security. Two invocation modes: ad-hoc natural-language subagent prompts, or structured recipe references. [Goose docs](https://block-goose.mintlify.app/concepts/agents), [Subagents](https://goose-docs.ai/docs/guides/context-engineering/subagents/), [DeepWiki recipes](https://deepwiki.com/aaif-goose/goose/4-recipes-and-scheduling)

**2. Claude Code — Markdown + YAML frontmatter** (subagents), plus separate ephemeral "Agent Teams":
```markdown
---
name: code-improver
description: Scans files and suggests improvements for readability, performance, and best practices
tools: Read, Grep, Glob
model: sonnet
---
<system prompt body — becomes the subagent's verbatim instructions>
```
Files live at `.claude/agents/<name>.md` (project) or `~/.claude/agents/<name>.md` (user); frontmatter declares name/description/tools/model/permissions, body = system prompt verbatim. Agent Teams (experimental, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`) are *not* config-file-defined — they're spawned via natural language at runtime, with a lead + teammates sharing a task list and mailbox (`~/.claude/teams/{team}/inboxes/{agent}.json`); a subagent definition can optionally be reused as a reusable teammate "role." [Sub-agents](https://code.claude.com/docs/en/sub-agents), [Agent Teams](https://code.claude.com/docs/en/agent-teams)

**3. CrewAI — split YAML (`agents.yaml` + `tasks.yaml`) + Python glue**
```yaml
# config/agents.yaml
researcher:
  role: "Senior Data Researcher"
  goal: "Uncover cutting-edge developments in {topic}"
  backstory: "..."
reporting_analyst:
  role: "Reporting Analyst"
  goal: "Create detailed reports based on {topic} data analysis"
```
`@CrewBase`-decorated `crew.py` loads both YAMLs and wires `@agent`/`@task` methods; `{topic}`-style templating resolves from crew inputs at runtime. [CrewAI YAML config](https://docs.crewai.com/en/concepts/agents), [DeepWiki](https://deepwiki.com/crewAIInc/crewAI/8.2-yaml-configuration)

**4. AGENTS.md — the emergent cross-vendor standard (no schema at all)**
Plain Markdown, no required fields, no frontmatter — headings/bullets only (build/test commands, code style, testing, security, commit rules). Originated at OpenAI for Codex, moved to the Agentic AI Foundation (Linux Foundation) in Q2 2026 for neutral stewardship; Codex, Cursor, Cline, Windsurf, Gemini CLI, and Grok Build all read the same file — 60,000+ repos use it. This is the opposite design bet from Goose/CrewAI: zero structure, maximal portability. [AGENTS.md guide](https://www.morphllm.com/agents-md-guide), [agents.md](https://agents.md/)

**5. OpenCode (sst) — JSON config + optional markdown agents**
`opencode.json`/`.jsonc` at `$HOME/.opencode.json` → `$XDG_CONFIG_HOME` → project-local, merged with project overriding global overriding remote-org defaults, and "managed settings" overriding everything (a governance hook similar to Claude Code's managed policy). Agents alternatively defined as `.opencode/agents/*.md` (frontmatter + body, same pattern as Claude Code). Per-agent fields: `model`, `prompt`, `permission` (a `Permission.Ruleset` for tool access), `steps` (max agentic iterations), `temperature`. [OpenCode docs](https://opencode.ai/docs/agents/), [Config](https://opencode.ai/docs/config/)

**Other notable formats**: Letta agents are created via API call (`client.agents.create()`) or visually in the **ADE** (Agent Development Environment) — a 3-panel IDE (Agent Simulator / Agent Configuration / Tools+Memory panels) rather than a config file; memory blocks and tools are edited live and versioned git-style. [Letta ADE](https://docs.letta.com/agent-development-environment/ade/). AutoGen/AG2 agents are Python objects (name, system message, LLM, tools) composable in a chat UI and exportable to Python code or JSON. Salesforce's new **Agent Script** gives Agentforce Builder a declarative+programmatic hybrid (graph-based, deterministic flow segments mixed with conversational segments) inside a single conversational build/test/deploy workspace — collapsing the old separate build→test→deploy cycle. [Agentforce Builder](https://admin.salesforce.com/blog/2026/build-with-confidence-inside-the-new-agentforce-builder). Grok Build deliberately reuses Claude Code/Codex conventions (AGENTS.md, MCP, skills, hooks) for zero-friction migration. [Grok Build guide](https://www.buildfastwithai.com/blogs/grok-build-xai-cli-ai-agents-2026)

**Marketplaces/templates**: Microsoft's **Agent Library** (curated, production-ready templates for Copilot Studio/M365/Power Platform, two types: Copilot Studio Agents vs. Declarative Agents) and the consumer-facing **Agent Store** in M365 Copilot (browse/install/publish, Microsoft + partner + customer agents) are the most mature "prebuilt agent marketplace" implementations found. [Agent Library](https://learn.microsoft.com/en-us/microsoft-copilot-studio/guidance/agent-library-overview), [Agent Store](https://devblogs.microsoft.com/microsoft365dev/introducing-the-agent-store-build-publish-and-discover-agents-in-microsoft-365-copilot/)

## (b) Agent-definition field matrix

| Field | Goose | Claude Code | CrewAI | AGENTS.md | OpenCode | Letta | Salesforce Agentforce |
|---|---|---|---|---|---|---|---|
| Name/description | ✅ `title` | ✅ frontmatter | ✅ role/goal/backstory | — (free text) | ✅ | ✅ | ✅ |
| System prompt | ✅ `instructions` | ✅ body | ✅ backstory | ✅ whole file | ✅ `prompt` | ✅ (editable in ADE) | ✅ Agent Script |
| Model + params | ✅ `settings.goose_model`, temp, max_turns | ✅ `model` field | via LLM config, not YAML | — | ✅ `model`, `temperature`, `steps` | ✅ (ADE config panel) | ✅ Atlas reasoning engine params |
| Tool/extension allowlist | ✅ `extensions` | ✅ `tools` | tools set in Python | — | ✅ `permission` ruleset | ✅ (Tools panel) | ✅ declarative actions |
| Permission tier | via extension restriction | ✅ per-subagent tools + inherited perm mode | — | — | ✅ `Permission.Ruleset` | — | ✅ guardrails |
| Memory config | — | — (session-scoped) | — | — | — | ✅ core/archival memory blocks, first-class | — |
| Triggers/schedule | ✅ (recipes are schedulable) | — | — | — | — | — | ✅ |
| Sub-agent access | ✅ `sub_recipes` | ✅ Teams/subagent reuse | crew composition | — | — | — | Agent Fabric multi-agent |
| Output schema | — | — | task `expected_output` | — | — | — | ✅ (Agent Script deterministic branches) |
| Guardrails | — | ✅ permission modes, hooks | — | — | ✅ permission ruleset | — | ✅ native to platform |

**Unique fields**: Letta is the only platform with memory as a first-class, independently-versioned config surface (git-style block versioning) — no other surveyed platform treats memory as structured config rather than session state. Claude Code and OpenCode are the only two with a *hook*-based enforcement layer (`TeammateIdle`, `TaskCreated`, `TaskCompleted` exit-code-2 gating) letting quality gates block agent actions programmatically. Salesforce is the only one offering a hybrid declarative/imperative single language (Agent Script) inside one canvas replacing separate build/test/deploy tooling.

## (c) Presentation / dashboard comparison

- **Claude Code Agent View** (research preview, May 2026): single CLI dashboard (`claude agents`) listing every background session — session ID, waiting-on-you flag, last response, last-interaction timestamp; sessions persist after terminal close via a supervisor process. Agent Teams uses a different, in-terminal "agent panel" (arrow-key select, Enter to open transcript, collapsed "N idle agents" row past 3 idle). No cost-per-agent in the panel itself — token cost is tracked plan-wide. [Agent View](https://www.buildfastwithai.com/blogs/claude-code-agent-view-guide), [Agent Teams](https://code.claude.com/docs/en/agent-teams)
- **Devin Desktop — Agent Command Center**: Kanban board (In Flight / Blocked / Ready for Review / Done) unifying local + cloud VM-isolated sessions; "Spaces" bundle tasks/sessions/PRs/files per project so agents share context instead of re-onboarding. [Devin Desktop](https://the-agent-report.com/2026/06/cognition-devin-desktop-agent-orchestration/)
- **Cursor**: unified "Agents Window" sidebar for every running agent (local + cloud), token usage per background task, Agent Tabs for parallel task review.
- **Pi Dashboard** (community, BlackBeltTechnology, not Pi's own product): browser dashboard mirroring live terminal sessions, send prompts/commands remotely, escalating-force kill, multi-session/multi-project view with token+cost tracking, integrated terminal, diff viewer, flow-graph view via "Flow Architect." Notable as the most kill-switch-explicit UI surveyed (graduated force levels, not just on/off). [PI Dashboard](https://www.agent-wars.com/news/2026-04-22-pi-dashboard-live-agent-control)
- **Goose Desktop**: Electron app; per-message usage stats (tokens, cost, TTFT, tok/s) with session-total rollups; sessions grouped by project in a nav panel — closer to a chat-history browser than a fleet dashboard.
- **Microsoft Agent 365**: enterprise-grade — a tenant-wide **agent registry** (single source of truth, auto-populated when agents are created in Copilot Studio) is the presentation layer, oriented around governance/compliance rather than live activity. [Agent registry](https://learn.microsoft.com/en-us/microsoft-agent-365/builder/agent-registry)
- **LangSmith/LangGraph Platform**: per-run trace view (every node/tool-call/LLM invocation with latency, tokens, full I/O), plus a management console for a versioned agent catalog across environments.

Pattern: coding-agent tools (Devin, Cursor, Claude Code, Pi) converge on a **Kanban/list-of-sessions** metaphor with live status + interrupt; enterprise platforms (Agent 365, Agentforce, Bedrock AgentCore) converge on a **registry/catalog** metaphor oriented at governance, not real-time activity.

## (d) Control-plane + governance comparison

- **Kill switches**: consensus 2026 best practice is a *layered* system living **outside the agent runtime** — session termination → permission revocation → circuit breaker → rollback → full deactivation — not a single button; global hard stop authenticated to an operator, revoking tool perms/halting queued jobs/locking pipelines within seconds. 65% of orgs surveyed say they've already had an agent incident; a majority said they could not reliably terminate a misbehaving agent. Okta for AI Agents explicitly offers "deactivate agents when they behave unexpectedly" as a governed action. [Kill-switch architecture](https://authoritygate.com/newsletter/kill-switch-gap-agentic-ai/), [Okta for AI Agents](https://www.okta.com/products/govern-ai-agent-identity/)
- **Budget caps / rate limits**: framed as "spend governors" that must sit in a control plane or gateway *before* the paid call is issued (not after-the-fact billing alerts) — cap tokens, API calls, RPM, and per-task budgets. Real 2026 failure cases cited: one enterprise burned $500M/month with no usage caps; Meta burned 60T tokens/85K staff/30 days. [Cost control](https://blog.laozhang.ai/en/posts/llm-agent-api-spend-kill-switch)
- **Scoping to directories/repos**: Devin's "Spaces," Cursor background agents (per-repo cloud clone), Grok Build (Git worktrees per subagent, up to 8 parallel), Claude Code subagents (declared `tools` allowlist) — directory/repo scoping is table stakes in coding agents specifically; general-purpose enterprise platforms (Agentforce, Copilot Studio) scope by data source/connector instead.
- **Model routing**: Amp routes Opus for long-horizon planning vs. Sonnet for default coding work; Claude Code Agent Teams lets teammates use a different model than the lead (or inherit it), configurable per-spawn or via a default-teammate-model setting.
- **Permission tiers**: OpenCode's config-merge hierarchy (project → global → remote-org → **managed settings**, which always wins) is the clearest worked example of dev→prod tiering inside one product; Claude Code teammates inherit the lead's permission mode at spawn time (can't be set per-teammate at spawn, only adjusted after) — a real, documented gap versus the "least privilege per agent" ideal.
- **Versioning/rollback**: LangGraph/LangSmith Deployment is the standout — centralized agent registry with versioning and *instant rollback*, a documented, GA capability others describe only aspirationally. [LangGraph Platform GA](https://www.langchain.com/blog/langgraph-platform-ga)
- **Approval to deploy / promotion**: Salesforce's new Agentforce Builder collapses separate build/test/deploy into one workspace (arguably weakening a formal gate in exchange for speed); Microsoft's model is the opposite — Copilot Studio agents auto-register into Agent 365's tenant-wide registry, creating "a governed path from prototype to enterprise deployment" with shared policy enforcement.

## (e) Governance, identity, and audit

- **Who can create an agent / approval workflow**: least mature area overall — mostly implicit (anyone with product access can create), with governance retrofitted via registries after the fact (Agent 365) rather than gated at creation time. No platform surveyed had a hard pre-creation approval gate as a native feature; this is closer to how CI/CD gated deploys 10 years ago than to a mature practice today.
- **Agent identity — the two real implementations**:
  - **Microsoft Entra Agent ID** (GA in 2026): introduces three concepts — **Agent Identity Blueprint** (reusable template), **Agent Identity** (the instance, a special service principal with no credentials of its own, authorized to *impersonate* the blueprint's identity), and **Agent User Account** (backing identity for on-behalf-of scenarios). Full discover/manage/monitor/secure lifecycle reusing existing Entra ID tooling. [What is Agent ID](https://learn.microsoft.com/en-us/entra/agent-id/what-is-microsoft-entra-agent-id), [Agent identities](https://learn.microsoft.com/en-us/entra/agent-id/agent-identities)
  - **Okta for AI Agents** (GA April 30, 2026): vendor-neutral, works with any IdP and specifically with **Amazon Bedrock AgentCore**-built agents; brings agents into standard human-style certification workflows — automated access reviews, human-owner assignment, permission-drift enforcement, full audit trail, and the ability to deactivate. Context: 91% of orgs already use AI agents but only 10% had a governance roadmap for non-human identities before this. [Okta for AI Agents](https://www.okta.com/products/govern-ai-agent-identity/), [Biometric Update coverage](https://www.biometricupdate.com/202605/okta-pushes-vendor-neutral-identity-governance-for-ai-agents)
  - **AWS Bedrock AgentCore Identity**: the underlying credential-management primitive both of the above interoperate with — brokered, short-lived credential vending for agents/tools to call AWS and third-party services on a user's behalf, now also accepting bring-your-own Secrets Manager ARNs (June 2026) for org-owned rotation/CMK/tagging policy. [AgentCore Identity](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html)
  - This is the single most important 2026 development for Arc's ADR-019 "Four Pillars" framing: two major vendors have now shipped **real, GA, non-human identity + governance planes** for agents, validating identity-as-first-pillar as an industry direction rather than a federal-only concern.
- **Registry/catalog + ownership/deprecation**: Microsoft Agent 365's tenant-wide registry and LangGraph Platform's versioned catalog are the two clearest "agent registry" implementations; both tie into audit (Agent 365 via Entra/Defender, LangSmith via trace history).
- **Audit of definition changes**: not clearly solved anywhere surveyed as a diff/blame view specific to agent *definitions* (as opposed to agent *runs*) — Letta's git-style memory-block versioning is the closest analog but scoped to memory, not the full agent definition.

## (f) What a SOTA 2026 agent control plane needs

Synthesizing across all 20 platforms, a state-of-the-art control plane must combine:
1. **Real non-human identity** (Entra Agent ID / Okta / AgentCore Identity pattern) — no shared credentials, short-lived brokered tokens, impersonation model, not bolted on post-hoc.
2. **A layered, out-of-band kill switch** — revoke → circuit-break → rollback → deactivate, controlled by an operator outside the agent's own runtime, with graduated force (Pi Dashboard's model) rather than binary.
3. **Pre-spend budget/rate governors** sitting in front of the provider call, not after-the-fact billing alerts (the $500M/month and 60T-token failures were both governance-gap failures, not technical ones).
4. **A live fleet view with per-agent status + interrupt** (Devin's Kanban / Claude Agent View / Cursor's Agents Window) *and* a governance registry (Agent 365 / LangGraph catalog) — these are two different surfaces solving two different jobs (operate vs. govern), and no platform surveyed unifies both well.
5. **Versioned, rollback-able agent definitions** (LangGraph's instant rollback is the reference implementation) with audit trail on the definition itself, not just its runs.
6. **A portable, low-friction definition format** for the actual behavior spec (AGENTS.md's cross-vendor convergence shows the market wants one shared minimal format even as richer YAML/JSON schemas — Goose recipes, CrewAI's split YAML, Salesforce Agent Script — compete for the "rich, structured, versionable" tier above it).
7. **Human-workflow parity for agent access** (Okta's insight): treat every agent like an employee — owner assignment, periodic access recertification, deactivation on anomaly — rather than a service account nobody reviews.

---
Sources are inlined above per claim. No claims in this report are marked [UNVERIFIED]; all are backed by at least one fetched/searched source URL from 2026 material.

## Addendum: Agent Identity Plane Interop

Researched 2026-07-25 via direct spec/doc fetches (session's WebSearch quota was exhausted; all findings below are from primary-source WebFetch of Microsoft Learn, AWS docs, MCP spec, A2A spec, IETF datatracker, and SPIFFE docs).

### 1. Protocol surface

**Entra Agent ID**: Entirely Microsoft Graph + Entra-native — no separate "agent protocol." An agent identity is a service principal object with an `id` (object ID = app ID), discoverable/manageable via the Microsoft Graph API (`federatedIdentityCredentials` resource, `accessPackageAssignmentRequest`, etc.). Third-party registration is real and documented, but only through Microsoft-defined integration points: Microsoft Foundry, Copilot Studio, Teams Developer Portal, or Azure App Service/Functions each auto-provision a blueprint + agent identity when an agent is created inside them. There is no documented generic "register any external agent" API for a platform that isn't one of those Microsoft surfaces — Foundry explicitly documents using the agent identity for MCP and A2A tool auth, so Foundry-hosted agents get Entra Agent ID "for free," but nothing exists for an agent hosted entirely outside Microsoft's stack to opt in directly. [Agent identities](https://learn.microsoft.com/en-us/entra/agent-id/agent-identities), [Agent ID governance](https://learn.microsoft.com/en-us/entra/id-governance/agent-id-governance-overview)

**Okta for AI Agents**: Marketing/blog material describes the promise ("every handoff is an access event that must be authorized, scoped, and logged," works with "any identity provider," interoperates with Bedrock AgentCore) but I could not locate public protocol-level API docs (REST/SCIM/OIDC endpoint specs) for third-party agent registration in the material fetched. **[UNVERIFIED]** beyond the vendor's own claim of being "vendor-neutral" — the actual integration contract (SCIM schema for agent objects? OIDC claims profile?) is not publicly documented at the depth this research could reach. Recommend a direct Okta developer-docs deep-dive (`developer.okta.com`) before making a build/buy call.

**AWS Bedrock AgentCore Identity**: The clearest documented surface. It ships a **fixed catalog of ~23 pre-built OAuth 2.0 providers** (Cognito, Auth0/Okta, Microsoft, Google, GitHub, Slack, Salesforce, CyberArk, Ping, OneLogin, etc.) for inbound and outbound agent auth — this is a credential-broker model, not an open federation protocol. There is also a separate "Connect to private identity providers" doc (not fetched in depth) suggesting custom/internal OIDC IdPs can be registered, which is the most promising lead for BYO federation but needs direct verification. [Provider setup](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity-idps.md), [Identity overview](https://docs.aws.amazon.com/bedrock-agentcore/latest/devguide/identity.html) — private-IdP specifics **[UNVERIFIED]**.

### 2. BYO-credential / federation — the crux question

**Entra Agent ID answers this cleanly, and the answer is asymmetric, not peer federation.** Agent identities themselves *never* hold credentials — "Agent identities don't have credentials of their own. They only authenticate using **federated identity credentials (FIC)**... The blueprint holds credentials that it uses to acquire tokens on behalf of agent identities." FIC is Microsoft's implementation of OAuth token-exchange trust (the same primitive used for GitHub Actions OIDC → Azure, or Kubernetes service-account-token → Azure federation) — an external OIDC issuer's tokens can be configured as a trust source that lets the *blueprint* mint an Entra-native token for the agent identity. **This means Arc's DID/Ed25519 keypair could plausibly become a *trust input* (an external OIDC-shaped issuer) that Entra federates from — but the output is always a single-tenant Entra access token.** Arc's DID does not become a portable, verifiable-anywhere credential; Entra becomes the token-minting authority of record inside that tenant. Federation here is "bring your trust, we still mint the identity," not "bring your identity." [Agent identities — Authorizing agent identities section](https://learn.microsoft.com/en-us/entra/agent-id/agent-identities)

No evidence found (Entra, Okta, or AgentCore docs) of any platform accepting a **W3C DID document or a raw external keypair directly as the credential of record** — every path routes through OAuth2/OIDC token exchange with the enterprise IdP as final issuer.

### 3. SPIFFE/SPIRE and W3C DID

**SPIFFE**: The core SPIFFE concepts documentation, as fetched, contains **zero mention of AI agents, LLM agents, or non-human agent identity** — it remains scoped to traditional workload identity (services, containers, VMs). No documented convergence between SPIFFE SVIDs and any of the agent-identity platforms surveyed (Entra Agent ID, Okta, AgentCore). This is a genuine negative finding, not a gap in this research: **as of the docs checked, SPIFFE has not extended into the AI-agent-identity conversation that Microsoft/Okta/AWS are having.** [SPIFFE concepts](https://spiffe.io/docs/latest/spiffe-about/spiffe-concepts/) — broader ecosystem activity (conference talks, vendor blog posts proposing SPIFFE-for-agents) may exist but is **[UNVERIFIED]** given exhausted search budget.

**W3C DID**: No DID mention in any of Entra Agent ID, Okta, AgentCore, MCP auth spec, or A2A auth model documentation fetched. Every 2026 enterprise agent-identity implementation found is **OAuth2/OIDC-shaped**, not DID-shaped. This is a meaningful signal: **the market is converging on OAuth2 token exchange + service-principal-style objects for agent identity, not on decentralized identifiers.** DID appears to be losing (or never entered) this particular fight as of mid-2026.

### 4. Standards in flight — who's winning

- **MCP Authorization spec** (fetched directly, `modelcontextprotocol.io`): fully OAuth 2.1-based. Stack = OAuth 2.1 (`draft-ietf-oauth-v2-1-13`) + RFC 8414 (AS Metadata) + RFC 7591 (Dynamic Client Registration) + RFC 9728 (Protected Resource Metadata) + RFC 8707 (Resource Indicators, mandatory, to prevent audience-confusion/token-passthrough attacks — explicitly called out as the "confused deputy" mitigation). **Important gap for Arc's thesis**: MCP's auth model treats the calling agent as an ordinary OAuth client acting "on behalf of a resource owner" (a human) — there is **no first-class concept of *which agent instance* made the call**, only which human/client authorized it. Arc's per-agent DID + signed ToolCall is solving a problem MCP's spec does not address at all. [MCP Authorization spec](https://modelcontextprotocol.io/specification/2025-06-18/basic/authorization)
- **A2A protocol**: explicitly punts — "A2A protocol payloads... don't carry user or client identity information directly. Identity is established at the transport/HTTP layer" and "delegates authentication to standard web mechanisms... OAuth2 and OpenID Connect." No DID, no mTLS mandate, no delegation-chain primitive. A2A would carry a DID-based bearer credential just fine if the transport layer chose to use one, but nothing in the spec rewards or expects it. [A2A enterprise-ready](https://a2a-protocol.org/latest/topics/enterprise-ready/)
- **IETF OAuth Identity Chaining** (`draft-ietf-oauth-identity-chaining`, v17, dated 2026-07-19, Proposed Standard, past IESG evaluation, in progress at RFC Editor): combines RFC 8693 (Token Exchange) + RFC 7523 (JWT Profile) to let identity/authorization context survive a request crossing multiple trust domains. **This is the closest thing to a real "on-behalf-of chain" standard in flight** — but it is general-purpose (multi-cloud, CI/CD, API security) and **explicitly does not address AI-agent-specific delegation scenarios** as of this draft version. [Identity Chaining draft](https://datatracker.ietf.org/doc/draft-ietf-oauth-identity-chaining/)
- Attempted verification of 1Password's "Cross-App Access (XAA)" proposal (an agent-delegated-OAuth-into-SaaS effort referenced in general industry commentary) — both the original and redirected blog URLs 404'd during this research; **[UNVERIFIED]**, needs direct follow-up if this becomes strategically relevant.
- **Net read**: OAuth 2.1 + token exchange/chaining is the clear winning substrate across every spec actually fetched (MCP, A2A, Entra FIC, AgentCore's provider catalog). Nothing AI-agent-specific has displaced it; the industry is extending existing OAuth/OIDC machinery rather than inventing an agent-native identity protocol.

### 5. Audit interop format

No format standard (CADF, OCSF, CloudEvents, or otherwise) was found specified by name in any Entra, Okta, or AgentCore doc fetched for agent activity/audit logs. Okta's material speaks only in outcome terms ("the audit log captures who authorized what," "evidence that can be produced for any auditor") without naming a wire format. Entra's governance model logs through existing Entra ID Protection / Microsoft Graph audit log schemas (proprietary to Microsoft Graph, not an open standard). **This is a genuine open gap**: there is no confirmed cross-vendor audit-log format for agent activity to target. If Arc wants to plug into enterprise SIEM/governance without replacing it, the safest bet given current evidence is emitting to **Microsoft Graph audit log shape** (for Entra/Agent 365 shops) and a **generic OCSF or CloudEvents envelope** (as the closest existing open standards in the broader security-observability space) rather than waiting for an agent-specific format to emerge — none exists yet. **[UNVERIFIED]** whether OCSF/CloudEvents are actually endorsed by Okta or Microsoft for this use case; this is this researcher's inference from the absence of a named standard, not a documented recommendation.

### 6. Honest verdict — federate or be the identity plane?

**Evidence for "federate into the existing IdP"**: Every enterprise-grade implementation found (Entra Agent ID, Okta for AI Agents, AgentCore Identity) is built explicitly so that **the enterprise's existing IdP remains the trust root and token-minting authority**, and all three integrate with each other rather than compete (Okta explicitly targets AgentCore-built agents; AgentCore ships Okta/Auth0/Microsoft as first-class providers). The buyer-side framing captured in the Okta coverage is stark: 91% of orgs already run agents, but only 10% had *any* governance roadmap — the demand is for **agents to show up inside the identity system the enterprise already audits (Entra/Okta/AD)**, not for a new identity system to learn. A platform that insists on being its own root of trust is asking a security team to add a second thing to govern, at the exact moment the whole 2026 narrative (Okta, Microsoft, AWS) is "stop having a second thing to govern."

**Evidence for "be the identity plane"**: Nobody surveyed — not Entra, not Okta, not AgentCore — actually solves *agent-instance-level* cryptographic identity (proving which specific agent process, not which OAuth client-app, made a call) the way Arc's per-agent Ed25519 keypair + signed ToolCall does. MCP's own spec has no answer for this; A2A punts entirely. Arc's crypto identity is solving a real problem the market's OAuth-shaped answer does not reach down to.

**Verdict**: this is not an either/or. The evidence supports **Arc keeping its own DID/keypair as the fine-grained, per-call cryptographic identity and audit anchor (which nothing else in the market provides), while federating outward via OAuth2/OIDC token exchange (the Entra FIC pattern, or the in-flight IETF identity-chaining draft) so that enterprise IdPs can see, certify, and — critically — deactivate Arc agents through their existing Entra/Okta workflows.** Competing head-on to *replace* Entra/Okta as the enterprise trust root is not supported by any 2026 evidence found — every serious vendor is consolidating around the incumbent IdP as root, not displacing it. The defensible, evidence-backed position is "Arc is the identity plane at the agent-instance/tool-call granularity that OAuth-shaped systems don't reach, and it federates upward into whatever IdP the enterprise already trusts" — sell the DID as the missing fine-grained layer underneath Entra/Okta, not as a competitor to them.

