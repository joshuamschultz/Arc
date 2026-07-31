# Buzz vs Arc: Agent Creation, Presentation, Control

## (a) Creation-format comparison — real examples

**Buzz — `.persona.md` (YAML frontmatter + markdown body = system prompt)**
`buzz-persona/src/persona.rs:95-170` defines `PersonaConfig`; parser at `persona.rs:208-259`.
Real file, `examples/meadow-core/agents/skip.persona.md:1-36`:
```yaml
---
name: skip
display_name: "Skip"
description: "Orchestrator — coordinates the team, delegates work, never builds."
subscribe: ["#general"]
triggers: { mentions: true, all_messages: true }
---
You are the orchestrator. You coordinate the team and keep the plan moving...
```
Personas ship in a **pack**, declared by `.plugin/plugin.json` (`buzz-persona/src/manifest.rs:79-121`), real example `examples/meadow-core/.plugin/plugin.json:1-25` — `id`, `personas: [...]`, `defaults: {model, temperature, triggers, thread_replies}`. The actual ACP binary (`buzz-agent`) is configured separately, entirely by **environment variables** (`BUZZ_AGENT_PROVIDER`, `ANTHROPIC_MODEL`, `BUZZ_AGENT_MAX_SESSIONS`, etc. — `buzz-agent/src/config.rs:736-830`). There is no identity/DID field anywhere in that config — process identity is a Nostr keypair held one layer up.

**Arc — `arcagent.toml` + sibling `arcllm.toml`/`arcrun.toml`, minted via `arc agent create`**
`arccli/commands/agent/create.py:21-80` scaffolds a directory, writes three composing config files, then **mints a DID immediately** (`create.py:93-112`, `arctrust.AgentIdentity.from_config`) and signs the scaffolded capability under it (`create.py:115-147`) before the agent can even run. Rendered template (`arccli/commands/agent/_common.py:96-` onward):
```toml
[agent]
name = "{name}"
org = "local"
type = "executor"
[identity]
did = "{did}"          # minted by `arc agent create`
key_dir = "~/.arcagent/keys"
[security]
tier = "{tier}"        # federal | enterprise | personal
...
```
Identity, tier, and signing posture are first-class config fields from birth; Buzz's persona has none of that — a persona is a prompt+trigger config, not an identity.

## (b) Agent-definition field matrix

| Field | Buzz (`.persona.md`) | Arc (`arcagent.toml`) |
|---|---|---|
| Identity | none (borrows the Nostr npub of whoever runs it) | `[identity].did`, minted + persisted at create |
| Model | `model: "provider:id"` string | `arcllm.toml [llm].model` |
| Tools/MCP | `mcp_servers: [...]` per persona | `capabilities/` dir, signed, AST-gated, tier-scoped imports |
| Scope | `subscribe` (channels) + `triggers` (mentions/keywords/all_messages) | policy pipeline (`arctrust.policy.PolicyPipeline`, first-DENY-wins) over every tool call |
| Security tier | none | `[security].tier` = federal/enterprise/personal, floors FIPS/signing/custody |
| Budgets/limits | none at persona level; process-wide env caps only (`max_context_tokens`, `max_parallel_tools=8`, `max_sessions` — unbounded by default, `config.rs:812`) | `[context]` prune/compact/emergency thresholds, `[security] loop_max_parallel`, circuit breakers (`runaway_max_repeat`, `error_cascade_max`) |
| Signing | none — plain YAML+MD file | required: capabilities signed under agent DID at create time |

## (c) Presentation / roster / admin

Buzz's VISION.md:21 promises "Agents — Directory. Your agents. Job board." That is implemented in the **desktop Tauri app**, not a server console: `desktop/src/app/routes/agents.tsx` + `desktop/src/features/agents`, backed by Rust commands `agents.rs`, `agent_config.rs`, `agent_logs.rs`, `agent_models.rs`, `agent_providers.rs`, `agent_discovery/`, and process lifecycle in `managed_agents/` (`backend.rs`, `discovery.rs`, `agent_snapshot.rs`, `agent_events.rs`). Kill is real SIGTERM→hard-kill (`backend.rs:99,133,140`; `discovery.rs:967,1106,1113`).

**`admin-web` is not agent admin.** `admin-web/src/types.ts` defines only `Report` and `FeedbackSummary`/`FeedbackDetail`; `App.tsx:790-797` nav has only `/reports` and `/feedback`. There is no server-side agent roster, no cross-device agent view, no operator agent-approval UI anywhere in Buzz.

Arc's `arcui/web/src/pages/agents.tsx` is a **fleet-wide web dashboard**: `AgentCard` (`agents.tsx:68-100`) shows DID, online status, session/schedule/policy-bullet counts, and a 24h token sparkline, backed by `arcui/src/arcui/routes/agents.py`, `agent_detail/`, `agent_sessions.py`. This is genuinely closer to a fleet-ops console than Buzz's per-desktop directory.

## (d) Control + governance

- **Concurrency:** VISION_AGENT.md:41/65 advertises "up to 8 concurrent sessions... configurable cap, default 8" but the actual env default (`config.rs:812`) is `usize::MAX` — doc/code mismatch, effectively unbounded unless the operator sets `BUZZ_AGENT_MAX_SESSIONS`.
- **Authorization:** Buzz's only scoping primitive is channel `subscribe` + message `triggers`; there is no tool allowlist/denylist, no rate limit, no budget ceiling at the persona level.
- **Admission/revocation:** the only admission gate in Buzz is `buzz-admin`'s relay **membership list** — `AddMember`/`RemoveMember` by pubkey+role (`admin`/`member`, never `owner`) (`buzz-admin/src/main.rs:41-72`). This governs humans and agents identically — there is no agent-specific approval, and no audit trail of persona-file edits (they're just file edits).
- **Pairing (`buzz-pairing-cli` + `buzz-pair-relay`):** this is **device key transfer**, not agent admission — NIP-AB QR+SAS handshake moves an existing nsec to a new device (`buzz-pairing-cli/src/main.rs:114-333`), over an ephemeral, unauthenticated, loopback-only relay with no persistence (`buzz-pair-relay/src/lib.rs:1-25`, 128-conn/4KiB/120s-TTL bounds). It doesn't create an identity or admit an agent to a community; VISION_AGENT.md:19-24 confirms community membership/jobs/DMs stay local to whichever relay URL the agent connects to.
- Arc governs continuously instead of at admission: every tool call passes the tier-aware `PolicyPipeline` (fail-closed on exceptions), circuit breakers (`runaway_max_repeat`, `error_cascade_max`), and blocked/risky calls park in `arcstore` for `arc approve` — a human, operator-key-signed grant (`approve.py:1-16`) that cannot be forged via chat.
- **Workflows:** `buzz-workflow/src/schema.rs:36-120` (triggers: message/reaction/diff/schedule/webhook; actions: send_message/send_dm/set_topic/add_reaction/call_webhook) has **no `requires_approval` field** despite VISION.md:123 claiming "every step traced." Arc's Mission Control task system has an explicit opt-in `requires_review` gate plus a dependency DAG.

## (e) What Arc must adopt
1. A real **fleet-wide agent directory experience** modeled on Buzz's `AgentCard` pattern is already ahead in `agents.tsx` — but Buzz's **pack** concept (one `plugin.json` bundling multiple personas + shared defaults + shared MCP config, `manifest.rs:79-121`) is a clean multi-agent-team packaging unit Arc has no equivalent for; blueprints are closest but not pack-portable.
2. Buzz's **channel subscribe/trigger** model is a simple, legible per-agent scoping primitive worth having as a lightweight companion to Arc's heavier PolicyPipeline for chat-surface agents.

## (f) What Arc does better
- Identity is minted and signed at creation, not borrowed from a human's keypair.
- Continuous policy+audit governance beats Buzz's admission-only (relay membership) model — Buzz has no per-agent revocation, no tool allowlist, no audit of persona edits.
- `arcprompt` (signed, sha256-identified, provenance-audited overlay prompts, fail-closed on bad signature) is categorically more governed than Buzz's plain-file `.persona.md` prompt body — no signature, no audit event, no tier awareness.
- Arc's approval gate (`arc approve`) is cryptographically bound to the deployment operator key and can't be spoofed via chat; Buzz has no equivalent for agent actions at all.
- Buzz's `admin-web` moderates content (reports/feedback) but has zero agent-governance surface server-side; all Buzz agent control is desktop-local and unaudited centrally.
