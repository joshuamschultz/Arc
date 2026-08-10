---
topic: "connector-extensions"
date: 2026-08-04
status: complete
---

# connector-extensions

## Inspiration

Three pressures landed at once, and Josh named all three as real.

**The fleet is stranded.** The five DGX agents can think, remember, message each other, and run workflows — but they cannot touch a single system where the work actually lives. No ticket, no doc, no mail, no calendar, no file. Smart and unable to act.

**It cannot be sold without this.** The product thesis is "anyone can turn arcagent into anything, quickly and non-technically." That claim is unprovable while the agent connects to nothing. Connectors are the demo.

**Josh needs them personally.** He wants his own agents working his Jira, his Confluence, his mail and calendar today. Personal use is what makes it useful, and everything else is downstream of that.

This is the 2026-07-26 ruling made real: MCP is the extensibility spine, and adding a capability should be configuration, not engineering.

## Projects

Installable connector extensions covering ten capabilities:

| Capability | What it reaches |
|---|---|
| Confluence | Spaces, pages, search |
| Jira | Issues, boards, comments |
| Gmail | Read, send, thread |
| Google Drive | Files, folders |
| Google Calendar | Events, availability |
| Outlook Mail | Read, send, thread |
| Outlook Calendar | Events, availability |
| OneDrive | Files, folders |
| Dropbox | Files, folders |
| 1Password | Items, vaults |

**Packaging follows the upstream, not a rule.** The default is one extension per service, so an agent that needs files does not also get mail. But if a good maintained connector covers a whole vendor workspace — Drive, mail, and calendar in one — we take it and ship it as one extension. The granularity we actually care about is enforced by per-tool allowlists, not by package boundaries. Ten capabilities is the floor; the extension count depends on what the vetted upstreams look like.

Underneath them: one finished MCP transport, shared by all. Each extension brings its own adapter, its own skills, its own tools, and its own setup instructions. Nothing about a specific vendor leaks into the shared path.

The mail extensions exist so an agent can own an inbox — receive, read, and send from it. Which inbox that is stays the operator's choice. The file extensions exist so an agent has somewhere to put what it creates.

## Audience

**Josh, today.** First and hardest user. Wants his agents in his own Jira, Confluence, mail, and calendar now. If it does not serve him it does not serve anyone.

**The DGX fleet.** Five agents (coder, josh, marketer, trader, sales) doing real work for real people. Each needs a different subset — the sales agent needs mail and calendar, the coder needs Jira and Confluence.

**Buyers of Arc.** Non-technical people evaluating whether an agent can do their job. They judge by what it can reach, not by its architecture. Their install has to be a CLI step and a paste, not an engineering project.

**Federal operators, later.** Not the first audience, but they must never be locked out by choices made now. Whatever ships has to be hardenable without a rewrite.

## Use Cases

**Install a connector.** Operator runs a CLI install step, Hermes/openclaw style. The extension declares what it needs. Operator fills in secrets and API details once. The agent stores them, installs the extension's skills and tools, and is live. No engineering.

**Agent owns an inbox.** Mail arrives addressed to the agent. It reads, decides, replies as itself. People correspond with it directly instead of through Josh.

**Agent files its own work.** It produces a document, a report, a deck — and drops it in Drive, OneDrive, or Dropbox where the human already looks. Work products land where work products live.

**Agent runs the ticket queue.** Reads Jira, comments, transitions, pulls context from the linked Confluence page, and reports what it did.

**Agent holds a schedule.** Reads calendar availability, books, reschedules, prepares for what is next.

**Fit a blueprint to a customer.** A blueprint declares which connectors its persona needs. Installing the blueprint installs them.

## Desired Outcomes

**An agent stops being a chat box and becomes a coworker.** The difference between advice and work done is whether it can reach the system.

**Adding a capability becomes configuration.** Someone non-technical adds a connector without touching Arc's code. That is the whole extensibility thesis, finally demonstrable.

**The MCP spine is finished and proven.** Not a docstring and an enum — a transport that dispatches, tested against ten real services. The half-wired state that has sat since ADR-018 is closed out.

**Blueprints become whole.** A persona can declare the systems it works in, so the sales blueprint ships a sales agent rather than half of one.

**Every external call still rides the envelope.** Signed call, caller identity, policy check, audit — for a Jira comment exactly as for a native tool. Arc becomes an MCP host that verifies what it loads and signs what it does, which no other host does.

**Josh's own work moves.** He measures this by whether his agents actually handle his tickets and his mail.

## Guiding Principles

**Only vetted upstreams.** Use existing maintained repos, but keep a short list we have actually read and pinned. If no upstream meets the bar for a service, we write that one ourselves rather than ship code we do not trust. Speed does not buy a pass here — these hold mail and vault credentials.

**The MCP path is DRY; adapters are per-extension.** One shared connector mechanism, identical for every service. Each extension carries its own adapter. Vendor specifics never leak into the shared path.

**Install once, then it is set.** CLI install, fill in what it asks for, agent stores it and wires its own skills and tools. The human step is bounded and obvious.

**Extensions, not modules.** These bolt on. Nothing gets baked into arcagent, arcllm, arcrun, or arcmemory.

**Never `*`.** Per-tool allowlists on every server. A wildcard is excessive agency (ASI02/LLM06) and is refused above personal tier. This is also what lets a broad vendor-wide connector stay safe — the allowlist, not the package boundary, decides what an agent can reach.

**Take the good upstream over the tidy taxonomy.** If one maintained connector covers a whole workspace well, that beats splitting it three ways for symmetry.

**The operator chooses the account.** Arc connects to whatever inbox or vault it is pointed at. Whose account it is, is not the product's business.

## Constraints

**MCP does not dispatch.** Verified on `develop` 2026-08-04: `TransportKind.MCP` exists at `arcagent/tools/_transport.py:31` and `mcp_servers` config at `core/config.py:194`. No code path calls an MCP server. Everything here sits on a transport that must be finished first.

**ADR-018 excluded MCP deliberately.** It has to be reversed, not ignored.

**Ten services, ten auth stories.** OAuth flows, token refresh, and scope models differ per vendor. Refresh cannot require a human every time.

**Credentials never touch the filesystem** (Arc standing rule), yet 1Password is a peer connector here, not the vault — so secret storage is Arc's own problem to solve.

**Federal tier must stay reachable** without a rewrite: signed artifacts, policy, audit at every seam from day one.

**Third-party code holds live credentials.** Supply chain (ASI04/LLM03) is the top risk in this project.

## Scope

**In:** Finishing MCP dispatch as a real transport. All ten capabilities reachable: Confluence, Jira, Gmail, Google Drive, Google Calendar, Outlook Mail, Outlook Calendar, OneDrive, Dropbox, 1Password — packaged as however many extensions the vetted upstreams naturally give us. One shared DRY MCP path with a per-extension adapter, skills, and tools. CLI install and setup flow where the operator supplies secrets once and the agent completes the rest. Per-server tool allowlists. Vetting and pinning each upstream we adopt. Signed-call, identity, policy, and audit coverage on every external call.

**Out:** Arc as an MCP *server* (being driven by other harnesses) — separate work. 1Password as the fleet credential vault — it is a peer connector here. Provisioning accounts, seats, or mailboxes for agents — the operator decides that. New gateway adapters or human surfaces. Any capability beyond the ten. Baking connector logic into arcagent or the other core packages. Writing our own MCP servers where a vetted upstream already exists.

## Open Questions

- Which specific upstream repo for each of the ten, and what is the vetting bar that lets one in? (research for `/deepen`)
- Which services genuinely have a maintained MCP server versus needing a direct API client adapter?
- Where do secrets actually live, given 1Password is a peer connector and credentials must not touch disk?
- How does token refresh work without paging a human each time?
- What does the CLI install command look like, and how much does it borrow from Hermes/openclaw?
- Which connector goes first as the proof of the finished transport?
- How do blueprints declare a connector dependency?
- If one upstream covers a whole vendor workspace, does the install flow still let an operator take only part of it?

**Resolved during this session:**
- Packaging → one per service is the default, but a good workspace-wide upstream is fine and preferred over splitting for symmetry. Ten capabilities is the floor; extension count follows the upstreams.
- Scope → finish MCP *and* ship all ten in one project.
- Upstream trust → only vetted upstreams; write our own if nothing meets the bar.
- 1Password's role → just another connector, not the vault.
- Agent identity → operator's choice; Arc only connects to what it is pointed at.

## Related Solutions

No matches in `.claude/solutions/` (only `security-issues` exists).

Prior art in project memory and brainstorms:

- **`project_arc_mcp_gap`** — Arc is neither MCP client nor server despite scaffolding that reads like both. Josh ruled 2026-07-26 that MCP is the extensibility spine. Textbook producers-unwired instance.
- **`project_blueprints_v2_mcp_spine`** — a blueprint is configuration only; extension ranking is MCP server → capability drop-in → skill → gateway adapter. Blueprints must allowlist tools per server.
- **`.claude/brainstorms/2026-07-26-sales-exec-blueprint.md`** — the blueprint design this unblocks.
- **`project_rapport_agentic_crm`** — external packages are capability drop-ins, not arcagent modules. Same boundary applies here.
- **`project_gateway_adapter_plugins`** — the entry-point plugin pattern used for gateway adapters is the closest existing shape to a connector extension.
- **`feedback_producers_unwired_pattern`** — Arc ships correct predicates with dead wiring. Demand end-to-end-through-real-path tests; never trust a self-report.
