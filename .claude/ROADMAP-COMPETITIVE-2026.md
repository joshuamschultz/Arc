# Arc Competitive Roadmap — 2026-07-25

**Purpose:** ground Arc's roadmap in a verified read of the 2026 agent-harness market and a
code-verified read of Arc's own state, then sequence the work that makes Arc the state-of-the-art
choice for enterprise + federal agentic deployment.

**Method — two rounds, 17 agents.**
*Round 1 (§1–§9):* nine agents. Eight surveyed the market (Claude Code, OpenCode, Grok Build, Pi,
Codex, Antigravity, Cursor, Amp, Devin, Cline, Aider, OpenHands, Warp, Factory, Replit, OpenClaw,
Hermes, Goose, and the enterprise/federal platform tier) via web research; one inventoried Arc from
its own source.
*Round 2 (§10–§11):* eight agents analyzing **Block Buzz** directly from a local clone against Arc's
source — the correct primary benchmark, missed in round 1.
Round 2 findings are source-verified rather than web-sourced, and are correspondingly more reliable.

**All 17 raw reports (~44k words, file:line evidence) are preserved in
`.claude/research/2026-07-competitive/`** — `research-01..09-*.md` (round 1, market)
and `buzz-01..08-*.md` (round 2, Buzz vs Arc).

> **Reliability caveat.** Most sources postdate this assistant's training cutoff. Every agent
> sourced claims with URLs and flagged `[UNVERIFIED]` items, but load-bearing external facts
> (certification status, GA dates, competitor metrics) must be spot-checked before they appear in
> a pitch deck, RFP response, or public README comparison.

> ## ⚠️ AMENDED 2026-07-25 — read §10 first
>
> The original pass researched the wrong primary benchmark. It surveyed Block's **Goose** when the
> actual competitor is Block's **Buzz** (`github.com/block/buzz`). Buzz was then analyzed directly
> from source by eight agents against Arc's source. **§10 is the corrected competitive analysis and
> supersedes parts of §1, §3, and §5.**
>
> What changed: the "control plane over harnesses" land grab in §1 is **already occupied** — Buzz
> ships an ACP harness driving Claude Code, Codex, and Goose today. The §3 moat claims all survive
> but for a *different and more precise reason* than originally written. The §5 fleet claim was
> overstated and is narrowed in §10.
>
> What did NOT change and remains valid: the table-stakes gap analysis (§4), the identity
> federate-upward decision (§9), and the Wave 0/1 sequencing (§6) — all hold independent of Buzz.

---

## 1. The strategic conclusion

> **AMENDED (§10):** the direction below is right but the framing is now wrong. "Control plane over
> harnesses" is *taken* — Buzz ships an ACP harness driving Claude Code, Codex, and Goose today.
> The corrected position is **the governed runtime**: the layer that decides whether a specific tool
> call may proceed and proves it afterward. Buzz drives other agents but cannot deny an individual
> tool call. Read §10.3.

**Arc should stop positioning as "a more secure coding agent" and start positioning as
"the accountable control plane for agent fleets — whoever's agent you run."**

Three research findings converge on this:

1. **Arc's security moat is real, narrow, and genuinely uncontested** — but it is not "security"
   generically. It is three specific mechanisms nobody ships (§3).
2. **Arc cannot win a head-to-head coding-UX fight** with Cursor/Claude Code/Factory, and doesn't
   need to. The market's clearest 2026 trend is the *meta-harness*: Warp hosting Claude Code +
   Codex + Gemini CLI + OpenCode in one pane, Vercel's AI SDK v7 `HarnessAgent` API, GitHub Agent
   HQ's Blackboard orchestration. These are control planes over other vendors' agents.
3. **Nobody has first-party fleet tooling.** Going from one agent on a laptop to N agents on a
   cluster is community Helm charts and duct tape everywhere except cloud-infra products
   (Bedrock AgentCore). This is the single largest unoccupied space adjacent to Arc's existing work.

The synthesis: **a meta-harness that runs other vendors' agents under signed policy, cryptographic
identity, and tamper-evident audit is a product no one currently sells.** Warp has the surface but
no governance. Palantir/Ask Sage have the accreditation but aren't agent harnesses. Arc has the
governance primitives already built and is missing the interop surface to apply them to anyone
else's agent.

That reframing also resolves Arc's biggest liability (§4): the missing MCP support stops being an
embarrassing gap and becomes the load-bearing feature of the strategy.

---

## 2. Arc's verified baseline

From code, not from spec status fields (`research-09`).

| | |
|---|---|
| Python source (non-test) | 118,417 LOC across 17 packages |
| arcui web | ~12,863 LOC TS/TSX |
| Tests | 978 `test_*.py` files |
| Packages | 18 (2 are not real code: `arcmas` = meta pip package, `arcmodel` = empty scaffold) |

**Solid:** `arcagent` (19 modules), `arcllm` (16 providers), `arcrun` (loop, sandbox,
checkpoint/resume, parallel dispatch), `arctrust` (DID, Ed25519 + FIPS ECDSA, 4-layer policy,
4 audit sinks incl. WORM chain), `arcmemory` (4 stores, analogical retrieval), `arcskill`
(SKILL.md + signing + improver + hub), `arcstore`, `arcteam`, `arcui` (12 pages / 19 route
modules), `arcprompt`, `arccli`.

**Partial:** `arcgateway` (embedded-only; standalone daemon intentionally disabled),
`arctui` (single screen), platform gateways (Slack/Telegram/Mattermost, ~600–1000 LOC each).

**Stub:** `arcmodel` (own README: "no public API yet" — routing logic actually lives in arcllm),
`arcmas` (meta-package; **not** a multi-agent framework despite the name).

### Corrections to internal assumptions

- **SPEC-056 Mission Control is shipped**, README still says DRAFT. Same for SPEC-055. Confirmed
  spec-status drift — the exact failure mode `project_spec_status_sync_requirement` warns about.
- **`arcagent/core` measures 6,328 LOC against a 3,500 budget.** Directionally over, though the
  measurement method differs from the budget script's globbing. Worth a real reconciliation.
- **MCP is scaffolded, not implemented** (see §4.1).

---

## 3. The moat — verified uncontested

> **AMENDED (§10.3):** all four claims survive contact with Buzz, but the reasoning below
> ("nobody else has these") is too weak to survive a technical buyer. **Use §10.3's framing
> instead** — the moat is about *where the trust boundary sits*, not about scarcity. Buzz signs
> what an agent SAYS; Arc signs what an agent DOES.

Each of these was checked across the full competitor set and came back empty. These are the claims
Arc can defend.

**3.1 Tamper-evident, hash-chained audit logs.**
Among Claude Enterprise, Copilot Enterprise, Cursor, Devin, Bedrock AgentCore, Gemini Enterprise,
and Agentforce — *none* publicly document cryptographically hash-chained or signed audit logs.
They document "audit logs," compliance APIs, and SIEM export, but not tamper-evidence as a designed
property. Tamper-evident logging exists only in niche vendors (Hashline, DeepInspect, OpenFang),
none of which are agent platforms. Arc has `arctrust/audit.py` + `witness.py` WORM chain today.

**3.2 Signed, authenticated inter-agent messages.**
No coding-agent product surveyed documents any cryptographic identity, signing, or authorization on
agent-to-agent messages. Isolation is achieved by *architecture* (separate VMs, sandboxes,
worktrees), never by *authenticated identity*. The sole cross-vendor exception is A2A's Signed
Agent Cards — and A2A explicitly leaves the authorization mechanism unspecified, so it covers
identity but not permission. Arc's `arcteam` (DID registry, signed messenger, NATS) is the only
implementation of this shape found.

**3.3 Signed extension supply chain.**
NSA/Microsoft 2026 guidance flags unsigned "pointer architecture" MCP registries and
Docker-escape-class sandbox failures as live risks. Claude Code's plugin manifest format has no
cryptographic signing requirement. Arc's SPEC-033 (signed skills via Sigstore + Rekor, AST scan)
targets precisely the risk the NSA is warning about.

**3.4 Supporting advantages**

- **No FedRAMP authorization exists for any of the 18 harnesses surveyed.** The federally
  authorized platforms (Palantir AIP, Ask Sage, Agentforce) are not agent harnesses. The market
  splits cleanly into *federally authorized but not tamper-evident-logged* vs.
  *tamper-evident-logged but not federally authorized*. Arc targets the empty intersection.
- **Native microVM isolation.** Every competitor buys Firecracker-class isolation from E2B, Daytona,
  or Modal. None build it. Arc's tier-routed sandbox (Firecracker → Docker → subprocess) is native.
  Note: this commoditizes fast — treat as a 12-month advantage, not a permanent one.
- **AI-activity forensics.** Cursor admits it logs admin actions but *not* prompt/tool-call detail,
  so "what did the agent actually read and do" cannot be reconstructed. No competitor volunteered a
  counter-claim. Arc's per-call signed ToolCall + audit emission answers a question the rest of the
  market currently cannot.
- **Memory isolation.** Most platforms ship a single org-wide knowledge base (Devin's Knowledge
  Base, typical Cognee deployments). Arc's DID-scoped, fail-closed memory is ahead of the shipping
  field — and Arc already survived the cross-agent bleed incident that architecture prevents.
- **Structural/analogical memory retrieval.** July 2026 research (arXiv 2511.21730) names
  structural retrieval for procedural memory as explicitly unsolved. arcmemory already implements it.
- **Agent-*instance* identity.** This is the sharpest version of the moat. The MCP auth spec is
  pure OAuth 2.1 and **has no concept of agent-instance identity** — only "an OAuth client acting
  on behalf of a human resource owner." A2A punts identity entirely to the transport layer. Entra,
  Okta, and AgentCore all authenticate *the agent as a directory principal*, not *which running
  process made this specific call*. Arc's per-agent Ed25519 + DID + per-call signed ToolCall answers
  a question no protocol or platform in the market currently asks. Everything else authenticates the
  actor; Arc authenticates the act.

---

## 4. The gaps — where Arc loses today

### 4.1 P0 — MCP client and server (blocking)

`interop` ranks MCP client as the **#1 table-stakes interop capability, difficulty: Low**. It is
near-universal; the only holdouts are Aider (open RFC) and Pi (omits by design, as an explicit
minimalism tradeoff).

Arc has `TransportKind.MCP` as an enum value, an `MCPServerEntry` config model, and a
`tool_registry.py` docstring claiming four transports. **There is no dispatch code.**
`ADR-018-no-mcp-no-migration-no-acp.md` excluded it deliberately.

That ADR should be revisited and, I recommend, reversed. It was a reasonable scope decision when
Arc was proving its core; it is now the single highest-leverage gap in the analysis, and under the
§1 strategy it is load-bearing rather than optional:

- **MCP client** = Arc agents can use the entire existing tool ecosystem.
- **MCP server** = *other* harnesses can drive Arc. Only Goose does this natively today
  (`goosed` REST+SSE). It is a genuine differentiator and the technical precondition for Arc
  governing other vendors' agents.

Arc's signing/policy/audit layer applied to MCP transport is also directly responsive to the NSA's
unsigned-registry warning — Arc would be the only MCP host that verifies what it loads.

SPEC-045 (scale + interop) is the existing home for this work and is currently pending.

### 4.2 P0 — cheap table stakes Arc simply lacks

| Gap | Why it matters | Difficulty |
|---|---|---|
| **AGENTS.md support** | Linux Foundation-governed, 60,000+ repos, 28+ tools. Universal expectation. Claude Code is the named holdout and takes flak for it. | Low |
| **Headless/exec mode, structured JSON/stream-JSON out** | Required for CI use and for any other agent to drive Arc. Precondition for §1. | Low–Med |
| **Checkpoint / rewind / session fork** | Claude Code's `/rewind` (dual restore: full revert vs. context-only compaction) is the recovery UX users now expect. **arcrun already has checkpoint/resume** — this is largely a surfacing job, not new machinery. | Low–Med |
| **Native background notifications** | The single most-patched gap in the market — a cottage industry exists purely to bolt this on (Pushary, ai-agent-notifier, AgentsRoom). Arc has gateways; wire completion/needs-input push through them. | Low–Med |
| **`arc doctor`** | Claude Code's `/checkup` evolved from pass/fail into an advisor that proposes and executes fixes. Cheap, high-perception. | Low |

### 4.3 P1 — parity work

- **Git worktree-per-agent.** Claude Code (native `--worktree`), OpenCode, and Hermes independently
  converged on this as *the* concurrency primitive. Isolation-by-construction beats Arc's current
  collision-detection approach. Affects arcrun parallel dispatch.
- **Three explicit permission modes** — Manual / guardrailed-Auto / hard spend-and-scope stop.
  Permission fatigue is the #1 repeated user complaint industry-wide; Claude Code's own data shows
  93% of prompts get approved anyway, which users read as theater. Arc has the policy pipeline —
  what's missing is the *UX* that makes it feel like protection rather than nagging. Cline's
  "Spend Limit Reached" hard stop is the reference pattern.
- **Blueprints → recipes parity.** Goose recipes are the most-praised feature in its community.
  Arc ships 3 blueprints; target 15–20 curated, git-diffable ones.
- **Trajectory-level evals.** Outcome-only evaluation passes 20–40% more cases than trajectory-level
  evaluation reveals. Arc has the audit primitive; pair it with step-level scoring. Also unblocks
  the currently-inert skill-improver golden gate.
- **Operator-facing trace view.** LangSmith's click-node→detail-panel and Temporal's
  pause/resume-live-stream + filter-failed-only are the reference patterns. arcui has the data.

### 4.4 P2 — strategic optionality

- **Zed ACP** (note: *Agent Client Protocol*, not IBM's ACP which merged into A2A). Buys Zed +
  JetBrains IDE distribution without building N plugins. Microsoft/VS Code declined it.
- **A2A bridge.** Mostly aspirational industry-wide — even Google hasn't fully shipped it in
  Antigravity. Cheap insurance given Linux Foundation convergence.
- **SKILL.md compatibility.** Converged across 15–30+ tools. Note "compatible" ranges from
  byte-identical to frontmatter-parsing-only.
- **OpenTelemetry GenAI semconv.** Real whitespace: *no coding agent emits these spans natively* —
  adoption is at the observability-vendor layer only. Cheap differentiation for enterprise buyers.
- **`arcmodel`.** Currently empty. Amp's multi-model router (routing subtasks to best-suited models)
  is the most distinctive implementation found and the obvious template.

---

## 5. The land grab — fleet control plane

> **AMENDED (§10):** this claim was overstated. Buzz *does* have first-party fleet tooling for
> hosting many **workspaces/tenants** (full Helm chart with HPA/PDB/NetworkPolicy, ArgoCD + Flux
> examples, formally-verified multi-tenant isolation) and it is better than Arc's. The surviving,
> narrower, and still-correct claim is: **nobody has first-party fleet-of-*agents* tooling** —
> orchestrating N agent *processes*. Buzz doesn't either: `buzz-agent` caps sessions per process,
> "ten agents in parallel" means manually running ten processes, and the Helm chart deploys
> `buzz-relay`, not agent workers. Use the narrow claim; the broad one is false.

**No product in the coding-agent segment has first-party fleet-of-agents tooling.** Claude Code fleet
deployments are community Helm charts and K8s operators written months after launch. Goose and
OpenHands have none. Bedrock AgentCore is the only first-party fleet story, and only because it is
cloud infrastructure rather than a portable tool.

Arc is unusually well-positioned: it already has `arcteam` (DID registry, signed messaging),
`arcstore` (tasks, approvals), Mission Control (shipped), the task reliability engine, and — as of
this branch — a single-image Docker deployment.

Target: `arc fleet up --agents 20` as a first-class, day-one command, with the arcui control plane
as the operator surface.

Two governance findings shape what that surface must include:

- **Kill switches are layered, not a button.** 2026 consensus is revoke → circuit-break → rollback
  → deactivate, sitting *outside* the agent runtime, operator-authenticated, graduated force.
  65% of orgs reported an agent incident; a majority could not reliably kill a misbehaving agent.
  SPEC-046 (rogue-agent) is the right home.
- **Budget governors must be pre-spend, in a gateway** — not post-hoc billing alerts. Cited
  failures include a $500M/month burn and Uber exhausting its annual AI budget in four months.
  Arc's spend controls should gate the call, not report on it.

Two more gaps nobody has closed, both cheap for Arc given its signing infrastructure:

- **Pre-creation approval** — who is *allowed* to create an agent. Governance everywhere is bolted
  on after the agent exists, via registries.
- **Definition-level audit/diff** — run-level tracing is well solved; nobody audits changes to an
  agent's own definition. Arc's arcprompt overlays pinned to the OPERATOR key are already most of
  the way there.

---

## 6. Sequenced build order

### Wave 0 — Unblock interop (highest leverage per unit effort)
1. Revisit/reverse ADR-018. Implement **MCP client** in `tool_registry.py` — wire the transport the
   enum already claims.
2. **AGENTS.md** ingestion alongside existing convention files.
3. **Headless exec mode** with structured JSON / stream-JSON output.
4. **`arc doctor`** — diagnose *and* fix.
5. Reconcile spec-status drift (SPEC-055/056 say DRAFT, code says shipped) and the core LOC budget.

### Wave 1 — Parity on what users notice daily
6. Checkpoint/rewind/fork UX surfacing arcrun's existing capability.
7. Three permission modes + hard spend-stop UI.
8. Native completion / needs-input notifications through existing gateways.
9. Worktree-per-agent isolation in arcrun parallel dispatch.
10. Expand blueprints to 15–20 curated recipes.
11. Operator trace view in arcui (click-node → detail; pause/filter live stream).

### Wave 2 — The land grab
12. **MCP server** — Arc becomes drivable by other harnesses.
13. **`arc fleet up`** — first-party fleet control plane.
14. **Meta-harness governance** — run Claude Code / Codex / Goose *under* Arc's policy + audit.
    This is the §1 strategy made concrete and the most defensible product Arc can build.
15. Layered kill switch (SPEC-046) + pre-spend budget governor.
16. Pre-creation approval gate + definition-level audit/diff.
17. **Arc OIDC issuer** — federate `arctrust` upward into Entra FIC / enterprise IdPs (§9).
    Removes the "parallel identity system" procurement objection.
18. **OCSF/CloudEvents audit envelope** — greenfield; no cross-vendor agent-audit format exists (§9).

### Wave 3 — Monetize the moat
19. ATO/FedRAMP package artifacts (SPEC-048/049/050) — the intersection nobody occupies.
20. Market the tamper-evident audit chain explicitly; it is a verified differentiator.
21. AI-BOM CI, FIPS evidence, standards mapping.
22. Trajectory-level evals + gated skill-improver promotion (never ungated self-modification —
    2026 evidence shows 46–74% proxy-gain-without-real-gain in self-improving code agents).

---

## 7. Open decisions requiring Josh

1. **~~Federate vs. compete on agent identity~~ — RESOLVED: federate upward.** See §9 for the
   evidence and the resulting work. Retained here only as a record of the decision.
2. **Reverse ADR-018?** Recommendation: yes. Rationale in §4.1.
3. **Is Arc a harness or a control plane over harnesses?** §1 argues the latter. This is the biggest
   positioning call and it reorders everything below it.
4. **Does "quick deploy" mean Docker-one-liner (current branch) or `arc fleet up` (§5)?** Both are
   worth building; the sequencing depends on whether the near-term buyer is an individual operator
   or an enterprise fleet owner.

---

## 9. Identity: federate upward, don't compete

**Decision: Arc federates into enterprise IdPs. It does not attempt to be the enterprise trust root.**

The evidence is one-sided:

- **Nobody accepts an external credential of record.** Entra, Okta, and AgentCore all route through
  OAuth2/OIDC with the enterprise IdP as final issuer. None accept a raw DID document or external
  keypair. Entra Agent ID has no generic "register any external agent" API at all — third-party
  registration only happens through Microsoft's own surfaces (Foundry, Copilot Studio, Teams,
  App Service/Functions).
- **But there is a clean federation seam.** Entra agent identities never hold credentials — only the
  *blueprint* does, via **Federated Identity Credentials (FIC)**, the same OIDC-token-exchange trust
  primitive behind GitHub Actions → Azure federation. Arc can stand up an OIDC issuer and become a
  trust input that lets the blueprint mint tokens. Arc's DID becomes a trust anchor Entra federates
  *from* — never a portable credential Entra federates *to*. Asymmetric, but sufficient.
- **DID is not part of this conversation.** Zero W3C DID mentions across Entra, Okta, AgentCore, MCP,
  or A2A docs. SPIFFE's core docs have zero AI-agent mentions — no convergence. The entire 2026
  enterprise agent-identity market is OAuth2/OIDC-shaped.
- **Demand is "let agents show up in the IdP we already audit."** 91% of orgs run agents; only 10%
  had a governance roadmap. These vendors interoperate rather than compete (Okta targets AgentCore
  agents; AgentCore ships Okta as a provider). A competing trust root would be selling against the
  grain of what buyers are asking for.

**The position this implies:** Arc is *the missing layer underneath* Entra/Okta, not their
competitor. They authenticate the agent as a principal; Arc authenticates each individual act. That
framing keeps the cryptographic advantage while removing the procurement objection — Arc stops
looking like a parallel identity system a CISO must adopt and starts looking like depth beneath the
one they already run.

**Resulting work:**

- **Arc OIDC issuer** so `arctrust` can federate into Entra FIC and equivalent OIDC trust inputs.
  This is the concrete integration; everything else in this section is positioning.
- **Track `draft-ietf-oauth-identity-chaining`** (v17, 2026-07-19, past IESG review) — Token Exchange
  (RFC8693) + JWT Profile (RFC7523) for cross-trust-domain on-behalf-of chains. General-purpose and
  explicitly *not* agent-specific yet, which is exactly the gap Arc's per-call signing fills. Worth
  watching for a standards contribution opportunity.
- **Audit envelope.** No cross-vendor agent-audit format exists — no CADF/OCSF/CloudEvents confirmed
  in any Entra/Okta/AgentCore doc. Recommend emitting a generic OCSF or CloudEvents envelope
  (closest existing open standards) plus Graph-shaped logs for Entra/Agent 365 shops. This is
  greenfield; Arc's hash-chained audit could plausibly define the format rather than adopt one.
- **Open item:** Okta's actual protocol contract (SCIM schema? OIDC claims profile?) could not be
  verified from public material. Needs a `developer.okta.com` deep-dive before implementation.

---

## 8. What Arc should explicitly *not* do

- **Don't chase coding-UX parity** with Cursor/Factory/Replit. Losing fight, wrong axis.
- **Don't ship ADD-only memory** (Mem0's 2026 tradeoff) — incompatible with AU-9/10/11 and with
  correcting a wrong memory once written.
- **Don't ship ungated self-modification.** Any skill evolution needs held-out, non-gameable evals
  plus gated promotion.
- **Don't trust environment-sourced memory writes.** eTAMP-class attacks poison once and exploit
  across every future session; route untrusted content through the existing trifecta gate.
- **Don't adopt a vendor eval GUI as system of record.** OpenAI shipped and retired Agent
  Builder/Evals inside a year; evals belong in code/CI.
- **Don't raise the core LOC ceiling again** to accommodate misplaced code — move the code.

---

# 10. Block Buzz — the actual competitor

**Amendment added 2026-07-25.** Eight agents analyzed `github.com/block/buzz` from a local clone
against Arc's source. Raw reports: `scratchpad/buzz-01..08-*.md`. Every claim below is file:line
verified in one or both repos, not inferred from marketing.

## 10.1 What Buzz is

> *"A workspace where humans and agents build together, on a relay you own."*

A self-hostable Nostr relay that is also a team workspace. Every message, reaction, workflow step,
review approval, and git event is a signed event in one log — same shape whether the author is a
person or a process. Agents are channel **members with their own keys**, not bots.

| | Buzz | Arc |
|---|---|---|
| Backing | Block, Inc. — release eng, K8s staging, signed/notarized builds, Buildkite CI | Solo/small team, local-only |
| Size | ~324k Rust (26 crates) + ~284k TS/TSX ≈ **608k LOC** | 118k LOC |
| Traction | 11.7k stars, 929 forks, 349 open PRs, 74 tagged releases | Not public |
| Tests | 342 Rust test files, **5,309** test fns, E2E suite | 978 test files |
| Formal methods | **TLA+ + Tamarin, machine-checked** | None |
| License | Apache 2.0 | Apache 2.0 |

Buzz is Block's internal Slack + GitHub + CI replacement, dogfooded and open-sourced. It is not a
demo. Treat it as the most mature project in this space.

## 10.2 The single most important fact

**Buzz contains zero mentions of federal, FedRAMP, NIST, CMMC, classification, or air-gap** across
its vision docs, SECURITY.md, and architecture doc. Buzz competes with **Slack + Discord + GitHub +
CI dashboards**. It is not competing with agent-governance frameworks, and it is not chasing
regulated buyers.

Arc's market is not the one Buzz is playing in. The danger is narrower and more specific — see 10.6.

## 10.3 The moat, corrected

All four claims survive. The original reasoning ("nobody else has these") was lazy and would not
have withstood a technical buyer. The correct framing is **where the trust boundary sits**:

| Claim | Verdict | The actual mechanism |
|---|---|---|
| Tamper-evident audit | **SURVIVES** | Buzz's chain (`buzz-audit/hash.rs:42-73`) is SHA-256 hash-linking with **no per-entry signature**, in mutable Postgres. Their SECURITY.md: *"the chain is keyless… tamper-evident but not tamper-resistant… an attacker with database write access can recompute the entire chain."* Arc's `WormSink` Ed25519-signs every record; `WitnessAnchor` anchors the head to separately-custodied storage — closing the exact gap Buzz documents. |
| Signed agent actions | **SURVIVES** | Traced in full: `agent.rs:295-348` → `invoke_tool_inner` → `McpRegistry::call` → raw `CallToolRequest` over MCP. **No signature, no attestation, no audit emission.** A tool call never touches `buzz-audit`. **Buzz signs what an agent SAYS (NIP-98/NIP-42, real per-request); Arc signs what an agent DOES.** The skipped layer is precisely what OWASP ASI02/LLM06 exploit. |
| Signed supply chain | **SURVIVES** | Arc: working Sigstore/cosign, Fulcio + Rekor (`arcskill/hub/verify.py`). Buzz: cargo-deny hygiene (several RUSTSEC advisories *waived*) + git commit signing. Nothing verifies agents, workflows, or MCP servers as loaded artifacts — MCP servers are unsigned local processes. |
| Cryptographic identity | **SURVIVES** | Buzz: static **per-account** Nostr keypair — no derivation, no TTL, no clearance narrowing; a persona (`persona.rs`, 645 lines) holds **no key material at all** and borrows the npub of whoever runs it. Arc: `derive_child_identity` — HKDF-SHA256, per-spawn nonce, monotone-non-increasing clearance, TTL-bound. |

**The decisive finding:** `buzz-agent` has **no per-tool-call authorization gate**. The only denials
in its dispatch path are technical — unknown tool name, dead MCP server, timeout. Never policy. A
Buzz agent's *local* tool use (shell, file edit, its persona's MCP servers) has no runtime
authorization at all. Buzz's own SECURITY.md: *"Channel membership is the only access control
mechanism"* — and that gate lives at the relay, not in the tool loop.

**Corrected one-line position:** *Buzz drives Claude Code, Codex, and Goose — but cannot deny a
single tool call any of them make. Arc can, and can prove it afterward.*

## 10.4 Where Buzz beats Arc — adopt or concede

1. **Formal verification.** `MultiTenantRelay.tla` (1,142 lines, TLC-checked: 472M states, 16.2M
   distinct, depth 13) + Tamarin proofs (`MultiTenantAuth.spthy`, 32 lemmas green). Non-goals
   explicitly scoped. Arc has zero formal-methods artifacts. **This is a checkable claim in a
   security review that Arc cannot currently make.** Highest-credibility gap on the list.
2. **Production deployment ladder.** Full Helm chart — HPA, PDB, ServiceMonitor, NetworkPolicy,
   HTTPRoute, `values.schema.json` — plus ArgoCD and Flux examples and a second chart for the push
   gateway. Arc has **zero** Kubernetes presence.
3. **Arc's scaling is stubbed shut.** `executor_nats.py` raises `NotImplementedError("no ETA")`,
   `pairing_postgres.py` is a stub, `arcstore` is SQLite-only. `arc fleet up --agents 20` is not
   possible even in principle until these land. **This blocks §5 entirely.**
4. **UI depth — a 19× gap.** Buzz ships ~290k UI LOC across four clients (desktop 229,634 TS/TSX;
   mobile 55,214 Dart; browser 4,145; admin 1,218). Arc ships ~15.3k (arcui web 12,619 + arctui
   2,699). Accessibility tracks it: Buzz 854 `aria-*` + 135 `role=` across 501 `.tsx` (~1.7/file);
   Arc 29 + 2 across 75 (~0.39/file). Buzz has reactions (personalized top-4 by frequency+recency),
   custom emoji, nested threads, a 9-mark rich composer, canvases, huddles with push-to-talk and
   *"add agent to a voice call"*, video-frame comments, inline diff cards with Unified/Split, and a
   real PR review surface with agents as reviewers. The seven-surface claim is **true** — all seven
   verified in `desktop/src/app/routes/`, plus four undocumented ones (Pulse feed, Projects,
   Reminders, Settings) and a multi-tenant CommunityRail switcher.
   **Mobile is far past a stub**: 13 pages, `channels/` alone is 77 files / 21,075 LOC, and
   `channels/agent_activity/` ships agent receipts *on the phone*.
5. **Push notifications + mobile.** Real push gateway (APNs, device attestation, signed leases) and
   a Flutter app. Arc has **zero** push infrastructure — grep-confirmed.
6. **The event-log substrate.** One kind-tagged signed log; every feature is a new `kind` integer
   inheriting audit/search/access **for free**. Arc has separate subsystems joined by convention —
   which is the structural cause of Arc's recurring producers-unwired bug class. Nothing in Arc can
   answer "show me everything that happened" as one timeline.
7. **Packs.** One `plugin.json` bundling multiple personas + shared defaults + MCP config
   (`manifest.rs:79-121`). A portable multi-agent-team unit. Arc's blueprints aren't pack-portable.
8. **Agent transcript taxonomy** worth copying into arcui: `ToolActivity`, `ThoughtActivity`,
   `PlanActivity`, `LifecycleActivity`, `RawEventRail`, `FileEditDiffView`, `ShellCommandBlock`.

## 10.5 Where Arc genuinely wins — verified, not asserted

1. **Per-tool-call authorization.** Arc's `PolicyPipeline` (first-DENY-wins, fail-closed) has no
   Buzz equivalent at any layer. This is the product.
2. **Sandboxed execution.** Buzz's SECURITY.md concedes the dev-mcp shell *"runs at the operator's
   trust level, like bash itself."* Arc's tier-routed Firecracker/Docker/subprocess directly answers
   ASI05. Buzz has no answer.
3. **Working approval gates.** Buzz's `WorkflowApprovalCard.tsx` is UI-complete but the executor
   never persists a pending token (`executor.rs:663` TODO; bug WF-08) — a run hitting it *fails*.
   Buzz's workflow schema has no `requires_approval` field at all. Arc's SPEC-035 approval is
   shipped, race-safe, and operator-key-bound so chat cannot forge it. **Perishable lead — ship the
   story now.**
4. **Identity at birth.** `arc agent create` mints a DID and signs the scaffolded capability before
   the agent can run. A `.persona.md` has no identity field.
5. **Continuous vs. admission-only governance.** Buzz gates once at relay membership; no per-agent
   revocation, no tool allowlist, no audit of persona edits (they're plain file edits).
6. **Air-gap.** One image, weights baked in, `HF_HUB_OFFLINE=1`, no Postgres/Redis/MinIO to secure.
   Buzz's HA path has a documented correctness caveat — NIP-98 replay protection is per-pod-scoped
   and needs a shared Redis seen-set operators must close manually.
7. **Solo time-to-value.** Arc: 2 commands, one container. Buzz: 4 commands + a multi-service stack.
8. **`arcprompt`.** Signed, sha256-identified, provenance-audited, fail-closed system prompts vs. a
   plain unsigned `.persona.md`.
9. **arcui's fleet dashboard beats Buzz's.** Buzz's `admin-web` is *not* agent admin — only
   `/reports` and `/feedback`. All Buzz agent control is desktop-local and centrally unaudited.
10. **Shipped inbound chat adapters** (Slack/Telegram/Mattermost). Buzz has no equivalent.

11. **Governance is a *visible surface* in Arc and invisible in Buzz.** This is the strongest UI
    finding and it inverts the "Buzz wins UI" story where it counts: **Buzz has a hash-chain audit
    log server-side and no UI for it at all.** Arc has `approvals.tsx` (blocked trifecta legs),
    `gated-capabilities.tsx` (unsigned-tool quarantine), `policy.tsx` (ACE bullets), and
    `security.tsx` (filterable audit trail). Arc also owns cost entirely — `arcllm.tsx` shows cost
    by provider *and* by agent with circuit breakers and budgets; **Buzz has no cost/token dashboard
    at all** (verified). Plus a 6-tab memory inspector, per-agent TOML editing, `SpawnLineage`,
    operator-mode gating on every write, and a TUI that runs headless over SSH.
12. **No kanban/job board exists anywhere in Buzz** — `agents.tsx` is a persona card-grid. Arc's
    Mission Control board is unique in this comparison.

*Two corrections to earlier premises in this document:*
- **"Branch-as-room" does not exist in Buzz.** Only a whole *project* binds to one channel
  (`features/projects/hooks.ts:79,213`) behind an "Open Discussion" button; branches are plain
  NIP-34 refs and `ChannelRouteScreen.tsx` has zero project awareness. VISION_PROJECTS.md's own
  status table marks it 📋 *designed*. The README's "branch as room" story is aspirational. A real
  GitHub-equivalent PR review UI **is** shipped, but beta-gated — credit that, not branch-as-room.
- *Discrepancy worth knowing:* VISION_AGENT.md advertises a default cap of 8 concurrent sessions;
  `config.rs:812` actually defaults to `usize::MAX`.

**An anti-pattern not to copy:** Buzz's approval card renders `null` once a decision is made — **no
decision history**. That is an AU-family control Arc must retain, not imitate.

## 10.5b The cleanest statement of the split — workspace vs. runtime

A 14-dimension feature map produced the sharpest framing available:

> **Buzz is a workspace with agents in it. Arc is an agent runtime.**

Buzz wins essentially every *workspace* dimension. Arc wins essentially every *agent-runtime*
dimension. The overlap is much smaller than the marketing surface suggests.

**Arc wins the runtime, verified:**

| Dimension | Arc | Buzz |
|---|---|---|
| LLM providers | **16 first-class**, ollama/vLLM native | **4** (`Anthropic`, `OpenAi`, `Databricks`, `DatabricksV2`, `config.rs:662-672`). "Local" is only an `OPENAI_COMPAT_BASE_URL` override, not an adapter |
| Agent memory | `arcmemory` 7,354 LOC — 4 stores, embeddings, analogical retrieval, agentic consolidation, LLM-confirmed dedup | NIP-AE "engrams" (`engram.rs`) — one encrypted `core` record + manual `mem/*` fetches. **No ranking, no semantic search, no consolidation, no dedup.** Not even listed in the Works-today table |
| Agent tools | bash/read/write/edit/grep/find/ls + browser (CDP + Browserbase) + web + voice + tasks + messaging | `shell`, `read_file`, `view_image`, `str_replace`, `todo` (+2 hook tools). **No web or browser tool at all** |
| Task orchestration | Dependency DAG, reliability watcher, retry/backoff, dead-letter, routing/review | Trigger→steps. No dependency graph, no retry/backoff semantics |
| Extensibility | `arcskill` + improver + hub, `create_tool`/`create_skill` self-modification | Persona packs only. No marketplace, no agent-authored capabilities |

**Buzz wins the workspace:** native multi-user chat (channels/threads/forum/DMs/huddles), git hosting
+ branch-as-room + code review, workspace-wide Cmd+K search across messages/patches/runs/approvals,
desktop + mobile, and a k8s/Helm multi-tenant story targeting 10K humans + 50K agents at ~600k
events/day.

**Why this matters strategically:** it means the §10.7 recommendation is not a retreat. Arc is not
a smaller Buzz — it is deeper than Buzz at the thing Buzz treats as a component. Every runtime
dimension above is one Buzz would have to build from scratch, and none of them appear in its vision
docs. The memory gap in particular is wide enough to be its own wedge: Buzz's agents effectively
don't remember, and nothing on their roadmap changes that.

## 10.6 The real threat, stated plainly

Not that Buzz replaces Arc. **It's that a buyer evaluating "agent identity and audit" sees Buzz's
signed-event-log, concludes the problem is solved, and never reaches the authorization question Arc
exists to answer.** Buzz is better-resourced, better-known, and tells a superficially identical
story with 11.7k stars behind it.

**Therefore: Arc must lead with policy, sandboxing, and compliance-tiering — never with generic
identity/audit.** Leading with identity/audit makes Arc read as a smaller, worse-resourced clone of
something Block already ships. This is a messaging constraint, not a feature request.

On current evidence Buzz will not grow a tool-authorization engine, sandboxed execution, or
compliance tiering — none appear anywhere in its vision docs.

## 10.7 Strategy: differentiate narrowly, integrate opportunistically

- **Rejected — compete head-on.** Rebuilding channels/canvases/git-hosting/mobile against a 608k-LOC
  Block-funded head start is not winnable and dilutes the actual differentiator.
- **Chosen — the governed runtime.** Policy pipeline, sandboxing, compliance tiering. Small surface,
  matches Arc's core-LOC discipline, serves a market Buzz explicitly isn't chasing.
- **Wedge — integrate with Buzz.** It's Apache 2.0 and protocol-native. An Arc-governed agent can sit
  behind a Buzz community as a first-class member, with Arc's PolicyPipeline deciding whether a tool
  call is allowed *before* the resulting signed event reaches the relay. Gives a regulated customer
  Buzz's UX with Arc's compliance underneath, and rides Buzz's traction instead of fighting it.
  Caveat: depends on Buzz's authorization staying coarse.

## 10.8 Roadmap deltas

**Promote to Wave 0:**
- Unblock scaling: Postgres backend for `arcstore`; implement or delete `executor_nats.py` and
  `pairing_postgres.py`. Nothing in §5 is possible until this lands.
- Reframe all external messaging around **authorization + sandboxing + tiering**, never identity/audit.

**Add to Wave 1:**
- Formal verification of Arc's policy pipeline (TLA+ or Tamarin) — closes the highest-credibility gap.
- Push notification infrastructure — flat capability gap; `buzz-push-gateway`'s attestation/lease
  pattern is a federal-appropriate reference.
- Event-log substrate for arcteam's agent-to-agent layer. Adopt now; retrofitting under enterprise
  deadline pressure is the expensive path.
- Helm chart matching Buzz's shape (HPA, PDB, ServiceMonitor, NetworkPolicy).
- Ship the approval-gate story publicly while Buzz's is still broken.

**arcui target spec (source-derived, ranked):**
- *Tier 1 (weeks):* live agent activity panel adopting Buzz's twelve render classes
  (`ThoughtActivity`, `PlanActivity` with plan-diff, `ToolActivity` with shell blocks / todo /
  file-diffs / images, lifecycle, suppressed) plus a toggleable raw event rail; presence with a
  **"Working in #x · 12s"** state replacing Arc's binary dot; global ⌘K search (**Arc already stores
  everything — only the index is missing**); reactions + threads on `messages.tsx` (~200 LOC, the
  strongest "colleagues work here" signal available per unit effort); accessibility pass to
  ≥1.5 aria/file + System theme + reduced motion.
- *Tier 2:* create/import/share agents from the browser — kills the `arc team register` CLI
  dependency; teams as a first-class UI object; room templates that seed agents.
- *Tier 3:* unified Governance inbox **with decision history**; audit-native join across
  `security.tsx`; workflow authoring — **but wire the executor first.** Buzz shipped the approval
  card before the executor and now prints an apology in its own UI
  (`WorkflowStepCard.tsx:31-37`: *"approval gates still stop runs with WF-08; approval records are
  not persisted yet"*). Arc's producers-unwired history says do the opposite.

**Add to Wave 2:**
- Packs — portable multi-agent-team bundles.
- Buzz integration wedge: Arc-governed agent as a Buzz community member.

**Explicitly do NOT build (UI):** huddles, media-frame comments, custom emoji, forum, Pulse-style
feed, communities, mobile. That is Buzz's consumer-collaboration surface and none of it advances
the federal-harness thesis.

**Explicitly do NOT build:** Nostr adoption (an unauthenticated pseudonymous federated relay as a
control-plane dependency regresses fail-closed policy and tamper-evident audit); a competing
collaboration/chat/forum/huddle layer; a git forge.

---

# 11. Arc = Buzz + more — the build plan

**Direction set by Josh 2026-07-25**, superseding §10.7's "don't build the collaboration layer."
Arc gets Buzz's workspace *and* keeps the runtime + governance depth Buzz doesn't have.

This is achievable, but only in one order. Built feature-by-feature it's a 200k-LOC slog. Built
substrate-first it's a fraction of that, because in Buzz's design **messaging, threads, reactions,
search, audit, and timeline are not six features — they are one substrate plus six `kind` integers.**

## 11.1 The enabling move: one signed event log

Everything below depends on this and almost nothing works cheaply without it.

Today Arc has separate subsystems joined by convention: `arcteam` messages, `arcstore` tasks,
`arcstore` approvals, `arctrust` audit. Nothing can answer "show me everything that happened."
Every new capability needs bespoke wiring — which is the structural cause of the producers-unwired
bug class ([[feedback_producers_unwired_pattern]]).

**Build:** a single append-only, kind-tagged, signed event log in `arcstore`, with `arcteam`,
tasks, approvals, and audit all writing to it as event kinds.

**Arc's version is strictly stronger than Buzz's**, and this is the whole "plus more" thesis in one
sentence:

| | Buzz's log | Arc's log |
|---|---|---|
| Per-event signature | ❌ keyless SHA-256 chain | ✅ Ed25519 per record |
| Tamper-resistance | ❌ *"an attacker with DB write access can recompute the chain"* | ✅ `WitnessAnchor` to separately-custodied storage |
| Authorization on write | ❌ channel membership at the relay only | ✅ `PolicyPipeline`, first-DENY-wins, per event |
| Identity | per-account static Nostr key | per-agent DID + per-spawn HKDF child, clearance-narrowing |
| Classification | ❌ none | ✅ classification-aware, no-write-down |

Once this lands: **search, threads, reactions, unread state, activity feeds, the audit UI, and the
agent timeline all become queries over one table** rather than six integrations.

## 11.2 Messaging UI — the screenshot, decomposed

Target: `channel-thread.png`. Arc's `messages.tsx` already has channels, mentions, a members
button, and a new-channel button, so this is an extension, not a rewrite.

| Element in the screenshot | Arc today | Work |
|---|---|---|
| `⌘K Search everything` | ❌ | §11.3 |
| Inbox (unified activity) | ❌ | Query over event log — trivial once 11.1 lands |
| Agents nav item | ✅ `agents.tsx` | Link it |
| Channel **sections** (The Hive / Product / Launch Swarm) | ❌ flat list | Small — grouping metadata on channel |
| Unread dots + DM unread counts | ❌ | Read-cursor event kind |
| `NEW` divider | ❌ | Falls out of read-cursor |
| Threads (`💬 1`) | ❌ | Event kind (parent ref) + split-pane/drawer |
| Reactions (`👀 1`, `🎬 2`) | ❌ | Event kind. **~200 LOC, strongest "colleagues work here" signal per unit effort** |
| Agent-mention pills (`@Fizz`, `@Honey`) | ✅ SPEC-055 mentions | Render as pills; tie to membership structurally |
| Rich composer (@ / attach / emoji / `Aa`) | plain input | Tiptap or Lexical + 9 marks |
| Markdown in agent replies (nested lists) | partial | Renderer pass |
| Header: member count, huddle, settings | partial | Member count cheap; **skip huddle** |
| Footer presence: `Honey: Working` | ❌ binary dot | Adopt Buzz's `AgentStatusBadge` model — **Working in #x · 12s**, Starting (15s grace), error-with-severity |

**Deliberately skipped:** huddles/voice, canvases, custom emoji, forum, Pulse feed, media-frame
comments, mobile. That's Buzz's consumer-collaboration surface; it doesn't advance the thesis and
it's where most of their 229k desktop LOC went.

Realistic scope for "feels like Slack, with agents in it": **~5–8k LOC of arcui**, not 229k.

## 11.3 Global search

**Arc already stores everything — only the index is missing.** This is the highest
value-to-effort item on the list.

- `⌘K` palette over the unified event log: messages, tasks, approvals, runs, tool calls, memories,
  audit records, skills, agents.
- SQLite FTS5 now; the Postgres backend (§10.8 Wave 0) upgrades it for free.
- **Permission-aware by construction** — filter by `caller_did` + classification through the
  existing policy layer. Buzz filters by channel membership; Arc filters by clearance. Same UX,
  stronger guarantee, and it's a federal selling point rather than a feature copy.

## 11.4 The "+ more" — what Buzz structurally cannot answer

Arc keeps everything from §10.5 and §10.5b. In a shared-workspace UI these become visible
differentiators rather than backend trivia:

1. **Governance as a surface.** Buzz has a hash-chain audit log and *no UI for it at all*, and no
   cost dashboard. Arc has `approvals.tsx`, `gated-capabilities.tsx`, `policy.tsx`, `security.tsx`,
   and cost by provider *and* agent. Put a Governance inbox in the sidebar next to Inbox —
   **with decision history**, which Buzz's card explicitly discards.
2. **Agents that remember.** arcmemory (4 stores, analogical retrieval, consolidation, dedup) vs.
   Buzz's engrams with no ranking, no semantic search, no consolidation. In-channel, this is the
   difference between an agent that answers *"have we seen this error before?"* with receipts and
   one that can't.
3. **16 providers incl. native local models** vs. 4. Air-gapped chat with agents is Arc-only.
4. **Per-tool-call authorization**, in a room where humans watch it happen. Every agent action in
   the channel is policy-gated and signed — visible provenance on each message.
5. **Task DAG + reliability engine** (retry/backoff/dead-letter/review) vs. trigger→steps.
6. **Working approval gates.** Buzz's executor still fails on `request_approval` (WF-08).
7. **Mission Control kanban** — no board exists anywhere in Buzz.

## 11.5 Build order

**Phase A — substrate (do first, everything depends on it).**
Unified signed event log in `arcstore`; migrate `arcteam` messages, tasks, approvals, audit onto it.
Do this *with* the Wave-0 Postgres backend — same migration, one disruption.

**Phase B — messaging parity.** Reactions → threads → read-cursor/unread/NEW → channel sections →
rich composer → agent presence (`Working in #x`) → mention pills. Ship in that order; each is
independently visible.

**Phase C — global search.** `⌘K` over the event log, permission-aware.

**Phase D — the differentiators.** Governance inbox with decision history; agent activity panel
(Buzz's twelve render classes + raw event rail); in-channel memory receipts; cost visible per agent.

**Phase E — surfaces, only if pulled.** Desktop (Tauri) then mobile. Do not start these until A–D
are solid; they multiply maintenance across every feature above.

**Non-negotiable, from Buzz's own mistake:** wire the executor before shipping the card. Buzz
shipped an approval UI with no backend and now prints an apology inside its own interface
(`WorkflowStepCard.tsx:31-37`). Arc's producers-unwired history makes this the single most likely
way this plan goes wrong.

## 11.6 Honest cost

This makes Arc's surface area much larger and directly contradicts the <3,500-LOC core discipline
in CLAUDE.md — though not fatally: all of it belongs in `arcui`, `arcstore`, and `arcteam`, none in
`arcagent/core`. Keep the core budget; let the workspace live at the edges.

The real risk is not LOC, it's focus: every hour on reactions is an hour not on formal verification,
k8s, or MCP. The mitigation is Phase A — because the substrate is *also* the FedRAMP audit story
(§10.8), the search index, and the structural cure for producers-unwired. It is the one piece of
work that pays for itself on both sides of the strategy.

---

# 12. New-session handoff — start here

**Written 2026-07-26.** This section is self-contained. A fresh session should be able to read
§12 alone and start work. Everything above is supporting evidence.

## 12.1 What happened and what was decided

Two research rounds (17 agents, ~44k words of sourced findings) compared Arc against the 2026
agent-harness market and then, correctly, against **Block Buzz** (`github.com/block/buzz`) — the
real competitor, an Apache-2.0 Nostr-relay workspace where "humans and agents build together."

**Decisions locked:**

1. **Build Arc = Buzz + more** (Josh, 2026-07-25). Arc gets the Slack-like collaboration surface
   *and* keeps its runtime + governance depth. This supersedes the earlier
   "differentiate narrowly, don't build a collaboration layer" recommendation.
2. **Positioning: "the governed runtime."** Not "control plane over harnesses" — Buzz already
   ships an ACP harness driving Claude Code, Codex, and Goose.
3. **Messaging constraint: lead with authorization + sandboxing + compliance-tiering. Never with
   generic identity/audit** — that's the story Buzz tells with 11.7k stars behind it.
4. **Identity: federate upward** into Entra/Okta via OIDC token exchange; don't compete as a trust
   root (§9).
5. **MCP is the extensibility spine** (Josh, 2026-07-26) — see `project_arc_mcp_gap` memory. The
   thesis is "anyone can turn arcagent into anything, quickly and non-technically," which only
   works if adding a capability is *configuration*, not engineering. MCP ranks ahead of authoring
   the first blueprint.

**The one-line competitive summary:** *Buzz signs what an agent SAYS; Arc signs what an agent DOES.*
Buzz's NIP-98/NIP-42 signing is real per-request, but its tool-call path
(`agent.rs:295-348` → `McpRegistry::call`) has no signature, no attestation, and never touches
`buzz-audit`. `buzz-agent` has **no per-tool-call authorization gate** — only technical denials.

## 12.2 File map

| What | Where |
|---|---|
| This roadmap | `.claude/ROADMAP-COMPETITIVE-2026.md` |
| 17 raw research reports (~44k words, file:line evidence) | `.claude/research/2026-07-competitive/` |
| Round 1 — market survey | `research-01..09-*.md` |
| Round 2 — Buzz vs Arc, source-verified | `buzz-01..08-*.md` |
| Most useful single report for §11 work | `buzz-06-uiux.md` (UI), `buzz-03-communication.md` (event model) |
| Arc's own verified inventory | `research-09-arc-inventory.md` |
| Buzz clone (temp — re-clone if gone) | `git clone --depth=1 https://github.com/block/buzz` |

## 12.3 IMPORTANT — Phase A is ~70% built already

**Do not build an event log from scratch.** A code read on 2026-07-26 found Arc already has most
of the substrate. The work is *extending and unifying*, not greenfield.

**What already exists:**

- **`arcstore/records.py`** — `SpoolRecord`: a flat, frozen Pydantic model, one JSON line per
  record. Already has `kind`, **`actor_did`**, `ts`, `request_id`, and a **content-derived
  `record_id`** for idempotent ingest. This is already a kind-tagged signed-ish event record.
  Current kinds: `SpoolKind = Literal["llm_call","run_event","agent_event","tool_event","spawn_event"]`.
- **`arcstore/backends/base.py`** — `StorageBackend` Protocol (async, `@runtime_checkable`):
  `start/stop/upsert/upsert_many`. Two implementations already conform (`sqlite.py`, `memory.py`),
  with a shared conformance suite proving no SQLite types leak into the contract.
  - `OPERATIONAL_TABLES` — one per spool kind, insert-once.
  - `AUDIT_TABLE = "audit_chain"` — the arctrust WORM mirror, per-row `verified` flag.
  - **`MUTABLE_RECORDS_TABLE = "mutable_records"`** — the mutable directory plane
    (SPEC-056 Phase 0A / SPEC-032), one collection per entity: tasks, entities, teams,
    **and channels**. Channels already exist as a directory entity.
  - `table_for_kind()` maps kind → table.
- **`arctrust/audit.py`** — `WormSink` with `_canonical_event_hash(seq, prev_hash, event)`,
  `chain_tip()`, `verify_chain()`, segment rotation, torn-record recovery, and
  `read_verified_anchor()`. Plus `AuditSink` Protocol, `NullSink`, and `emit()` as the single
  emission point. **This is already better than Buzz's keyless chain.**

**So Phase A reduces to four concrete jobs:**

1. **Extend `SpoolKind`** with conversation kinds — `message`, `reaction`, `thread_reply`,
   `read_cursor`, `presence`. Add the fields they need to `SpoolRecord` (it's flat by design, so
   this is additive optional fields, matching how `cache_read_tokens` was added for SPEC-029).
   Add table mappings in `_KIND_TABLE` / `OPERATIONAL_TABLES`.
2. **Route `arcteam` messages through the spool** instead of its own path, so every message is an
   event with `actor_did`, policy-gated on write and mirrored into `audit_chain`.
3. **Unify the read path** — one query API over operational + mutable + audit tables. This is what
   makes global search (§11.3), the Inbox, threads, and the agent timeline all one thing.
4. **Postgres backend** implementing `StorageBackend`. Already scaffolded as "future network
   backends" in the Protocol docstring. **This is also Wave-0 scaling work** (§10.8) — same
   migration, one disruption. Note `backends/__init__.py` raises `NotImplementedError` for unknown
   backend names today.

**Verify before building** (the inventory is a day old and Arc moves fast):
```bash
rg "SpoolKind|_KIND_TABLE|OPERATIONAL_TABLES" packages/arcstore/src
rg "class .*Sink|def emit" packages/arctrust/src/arctrust/audit.py
rg "mutable_records|MUTABLE_RECORDS_TABLE" packages/arcstore/src
sed -n '1,80p' packages/arcteam/src/arcteam/messenger.py
```

**Why Arc's version beats Buzz's** — keep this in the spec, it's the "+ more":

| | Buzz | Arc |
|---|---|---|
| Per-event signature | keyless SHA-256 chain | Ed25519 per record (`WormSink`) |
| Tamper-resistance | *their SECURITY.md:* "an attacker with database write access can recompute the entire chain" | `WitnessAnchor` → separately-custodied append-only medium |
| Authorization on write | channel membership, at the relay only | `PolicyPipeline`, per event, first-DENY-wins, fail-closed |
| Identity | static per-account Nostr key | per-agent DID + per-spawn HKDF child, monotone clearance narrowing, TTL |
| Classification | none | classification-aware, no-write-down |

## 12.4 Phase B — messaging UI, decomposed

Target is Buzz's `docs/assets/screenshots/channel-thread.png`. Arc's `packages/arcui/web/src/pages/messages.tsx`
(419 LOC) already has `#channels`, mentions, `MembersButton`, `NewChannelButton`, and operator-mode
gating — so this is extension, not rewrite.

Ship in this order (each independently visible, cheapest-first):

1. **Reactions** — new event kind. ~200 LOC. The strongest "colleagues work here" signal per unit
   of effort. Buzz ranks its quick-picker by frequency+recency; worth copying.
2. **Threads** — event kind with a parent ref; split-pane ↔ drawer toggle.
3. **Read cursor → unread dots, DM unread counts, the `NEW` divider.** One event kind gives all three.
4. **Channel sections** (Buzz's "The Hive / Product / Launch Swarm") — grouping metadata on the
   channel directory entity, which already exists in `mutable_records`.
5. **Rich composer** — Tiptap or Lexical, 9 marks, `@` autocomplete, emoji, attach.
6. **Agent presence** — replace Arc's binary `StatusDot` with Buzz's model from
   `AgentStatusBadge.tsx:9-56`: **Working in #x · 12s** (pulsing, per-channel), **Starting…**
   (amber, 15s grace before flagging a stuck start), active, error-with-severity. Buzz computes it
   by merging observer turn events with a typing fallback.
7. **Agent-mention pills** — render `@Fizz` as a chip; tie mentions to channel membership
   *structurally* (Arc's `arcteam/mentions.py` regex+DID-resolve is currently disconnected from
   `Channel.members` checks in `messenger.py` — fix that seam here).
8. **Markdown rendering** in agent replies (nested lists, code blocks).

**Explicitly do not build:** huddles/voice, canvases, custom emoji, forum, Pulse-style feed,
media-frame comments, mobile. That's Buzz's consumer-collaboration surface, where most of their
229k desktop LOC went, and none of it advances the federal-harness thesis.

**Realistic scope for "feels like Slack, with agents in it": ~5–8k LOC of arcui.**

## 12.5 Phase C — global search

**Best value-to-effort item on the entire roadmap: Arc already stores everything; only the index
is missing.**

- `⌘K` palette over the unified read path: messages, tasks, approvals, runs, tool calls, memories,
  audit records, skills, agents.
- SQLite **FTS5** now; the Postgres backend upgrades it for free.
- **Permission-aware by construction** — filter by `caller_did` + classification through the
  existing policy layer. Buzz filters by channel membership; Arc filters by clearance. Same UX,
  strictly stronger guarantee, and it reads as a federal capability rather than a copied feature.

## 12.6 Phase D — the "+ more" (what Buzz structurally cannot answer)

1. **Governance as a sidebar surface, next to Inbox.** Buzz has a hash-chain audit log and **no UI
   for it at all**, and **no cost dashboard**. Arc has `approvals.tsx`, `gated-capabilities.tsx`,
   `policy.tsx`, `security.tsx`, and cost by provider *and* agent in `arcllm.tsx`.
   **Include decision history** — Buzz's approval card renders `null` once decided, discarding it.
   That's an AU-family control; keep it.
2. **Agent activity panel** — adopt Buzz's twelve render classes (`ThoughtActivity`, `PlanActivity`
   with plan-diff, `ToolActivity` with shell blocks / todo / file-diffs / images, lifecycle,
   suppressed) plus a toggleable **raw event rail**.
3. **In-channel memory receipts** — arcmemory (4 stores, analogical retrieval, consolidation,
   LLM-confirmed dedup) vs. Buzz's engrams (no ranking, no semantic search, no consolidation).
   In a channel this is the difference between answering *"have we seen this error before?"* with
   receipts and not being able to.
4. **Per-tool-call policy visible in-room** — provenance on every agent message.
5. **Mission Control kanban** — no board exists anywhere in Buzz.

## 12.7 Arc-specific traps (read before writing code)

- **Producers-unwired is the #1 risk to this plan.** Arc repeatedly ships correct predicates with
  dead activating wiring. Buzz made the mirror-image mistake — shipped an approval card with no
  executor and now prints an apology in its own UI (`WorkflowStepCard.tsx:31-37`).
  **Wire the executor before shipping the card.** Demand E2E-through-real-path tests.
- **Parallel frontend work clobbers the committed bundle.** Multiple shells editing `arcui/web`
  contend on the static build. Worktree-isolate frontend agents, or commit each feature's source
  separately and do one coordinated build after all web source is committed.
- **Spec status drift.** SPEC-055 and SPEC-056 say DRAFT but shipped. Never trust a status field;
  verify in code.
- **Core LOC budget.** `arcagent/core` measures ~6,328 against a 3,500 ceiling. Everything in §11
  belongs in `arcui`/`arcstore`/`arcteam` — **none in `arcagent/core`**. Don't raise the ceiling;
  move the code.
- **Scaling is stubbed shut.** `arcgateway/executor_nats.py` raises `NotImplementedError("no ETA")`,
  `pairing_postgres.py` is a stub, `arcstore` is SQLite-only. Fleet work is blocked until these land
  — which is why the Postgres backend is folded into Phase A.
- **Don't adopt Nostr.** An unauthenticated pseudonymous federated relay as a control-plane
  dependency regresses fail-closed policy and tamper-evident audit.

## 12.8 Suggested first session

```
Phase A, job 1+2 only:
  1. rg the verification commands in §12.3 — confirm the substrate is as described
  2. Extend SpoolKind with `message` + `reaction`; add fields to SpoolRecord
  3. Route arcteam messenger writes through the spool (policy-gated, audit-mirrored)
  4. E2E test through the REAL path: send a message → assert it lands in the
     operational table AND audit_chain AND was policy-evaluated
  5. Then, and only then, the arcui reactions UI
```

Gate every step on: `ruff check .` · `mypy --strict` · `pytest` · core-LOC check.

## 12.9 Open questions for Josh

1. **Desktop/mobile (Phase E)** — build, or stay web + TUI? Buzz has Tauri desktop + Flutter mobile.
   These multiply maintenance across every feature in §12.4.
2. **Does `arc fleet up` survive** the shift to a collaboration product, or does multi-tenant
   (Buzz-style communities) replace fleet-of-agents as the scaling story?
3. **Formal verification** — Buzz has TLA+ (472M states model-checked) + Tamarin. It's the one
   claim they can make in a security review that Arc can't. Worth the investment now, or later?
4. **MCP sequencing** — decided as the extensibility spine (2026-07-26). Does it land before,
   during, or after Phase A? It's independent work but competes for the same hours.
