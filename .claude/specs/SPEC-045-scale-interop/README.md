# SPEC-045 — Scale + Interop

**Feature:** Five hardening/interop deliverables that make Arc's transport, ingress, identity, and tool-interop surfaces production-real: (1) mTLS + signed/replay-protected NATS for inter-agent comms, (2) ingress rate + per-conversation turn/token budgets, (3) real platform→agent DID routing, (4) an MCP client + server with anti-tool-poisoning, and (5) arming the dormant SPEC-035 lethal-trifecta gate by tagging + EgressProxy-routing all network/egress tools.
**Status:** PENDING (planning only — PRD/SDD/PLAN written, no code)
**Type:** Generic (transport hardening + ingress guardrails + identity routing + tool-interop)
**Docs:** [PRD](./PRD.md) · [SDD](./SDD.md) · [PLAN](./PLAN.md)
**Depends on / reuses:** SPEC-034 (policy pipeline), SPEC-035 (lethal trifecta + EgressProxy), SPEC-036 (sandbox), SPEC-038 (classification ladder + budgets), SPEC-037 (FIPS signing — federal TLS), ADR-019 (tier = stringency), ADR-004 (LOC budgets), CLAUDE.md Four Pillars + OWASP LLM01/LLM03/LLM10, ASI03/ASI04/ASI07.

---

## One-liner

Inter-agent NATS runs in plaintext, ingress has no rate/turn budget, Slack/Mattermost route to a broken/empty agent DID, Arc has no real MCP, and the lethal-trifecta gate is armed-but-dormant because no built-in emits `external_comms`. SPEC-045 closes all five by **wiring existing seams** — arctrust TLS+signing primitives on the live bus, a gateway IngressGuard, real DID stamping/resolution, an arcagent MCP-client transport that flows through the unchanged policy/classification/EgressProxy choke point, and EgressProxy-routing the platform replies + MCP calls that finally arm the trifecta.

## Investigation findings (built-vs-unwired — all confirmed at file:line)

- **NATSExecutor is a dead stub.** Two identical `NotImplementedError` copies (`arcgateway/executor.py:326`, `executor_nats.py:17`), never instantiated. Real executors: `AsyncioExecutor` (in-process) / `SubprocessExecutor` (federal). **The real ASI07 gap is the *live* bus** — `arcteam.backends.nats.NatsBackend.connect()` + the arcagent messaging module carry actual agent-to-agent traffic over `nats.connect(servers)` with **no TLS / no signing / no replay**. Only TLS ref in the repo is `arcllm/modules/otel.py`.
- **agent_did routing is placeholder/broken.** `GatewaySection.agent_did = "did:arc:agent:default"` matches no real agent. Slack + Mattermost adapters hardcode `agent_did=""` and never accept a DID; Telegram uses the placeholder; `bootstrap.py:218` passes bare `gateway.agent_did` (not `effective_agent_did(platform)`). Result: Slack/MM messages fail to resolve (`FileNotFoundError` → `[agent-error]`); no per-conversation routing exists.
- **pairing_throttle only throttles pairing.** Per-user code-mint window + per-platform pending cap + lockout — **not** message ingress, and no per-conversation turn budget. Budgets exist only at `ProviderLayer` (per-provider) + arcrun `RunState.max_turns` (per-run). **No cross-turn ingress budget authority.**
- **No real MCP.** `ToolTransport.MCP` enum + `MCPServerEntry` config exist but `mcp_servers` is consumed nowhere in production (config tests only). No client, server, dispatch branch, or SDK dep.
- **Trifecta gate dormant.** Correct + tested, but only `file_read` (private_data) + `subprocess` (untrusted_input) produce legs; **no built-in emits `external_comms`**, so the gate never trips. `EgressProxy` + `TAG_TO_LEGS` + `HumanGate` are the live seams to feed.

## Key design decisions

- **MCP client → arcagent** (a tool provider; implements the existing `ToolTransport.MCP`; `mcp_servers` config already there). **Not arcllm** — MCP never touches the LLM wire.
- **MCP server → arcgateway** as a `arcgateway-mcp` entry-point plugin (ingress surface; core names only `web`). Gated by OQ-2.
- **Anti-tool-poisoning = sanitize + pin.** Boundary-mark untrusted descriptions before prompt use (LLM01); pin `sha256(name||schema||description)` on first sight and deny unapproved change (rug-pull / ASI04); federal = operator-signed allowlist, unpinned = deny.
- **mTLS design.** arctrust owns the single `nats_tls_context` factory + Ed25519 envelope sign/verify + windowed replay-nonce; arcteam/arcgateway are consumers. Channel encryption (mTLS) + end-to-end message auth (signing) are both required.
- **Trifecta legs.** New `"mcp": {external_comms, untrusted_input}` in `TAG_TO_LEGS`; ingress marks `untrusted_input`; platform replies + MCP egress route through `EgressProxy` → gate becomes live (proven with real tools, per the SPEC-035 review lesson).
- **Reuse, don't rebuild.** MCP tools + ingress `ToolCall`s flow through the unchanged `ToolRegistry._create_wrapped_execute` → `PolicyPipeline.evaluate` → ClassificationLayer/ProviderLayer choke point. No new policy engine, ladder, budget accountant, or sink.

## Concern-boundary split

`arctrust` = crypto primitives (TLS ctx, envelope sign/replay; policy + classification already there) · `arcteam` = secured NATS backend · `arcgateway` = NATSExecutor, IngressGuard, DID routing, MCP server, adapter egress · `arcagent` = MCP client transport + anti-poisoning + leg-tagging · `arcllm` = **nothing** · `arcrun` = unchanged.

## Open questions (product owner)

- **OQ-1:** Implement `NATSExecutor` for real (multi-replica) now, or delete the dead stub and land only mTLS+signing on the live arcteam bus? *(Default: secure live bus now; executor only if replica scaling is imminent.)*
- **OQ-2:** Ship the MCP **server** now, or **client-first** and defer the server? *(Default: client-first — consuming external tools is the immediate need; exposing Arc's tools is a larger attack surface.)*
- **OQ-3:** Per-conversation `(platform, chat_id) → agent` config shape — TOML routing table vs resolver hook?
