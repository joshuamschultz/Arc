# Note: Cross-Host Multi-Agent Security — what we need & why

> Status: parked / future work. Not a spec — a memo so we can pick this up later
> with the reasoning intact. Written 2026-06-13.

## TL;DR

In-process per-call signing (arcagent ↔ arcrun ↔ arcllm in one process) is
**security theater** — same memory, same key, so a compromised process just
forges its own signatures. We already do the right thing in-process: session
DID + PolicyPipeline + audit. The **real** unsolved security work is at the
boundaries calls actually cross: **agent-to-agent across hosts**. Two pieces:

1. **NATS mTLS** — authenticate the transport between agents on different hosts.
2. **Capability-token delegation (Biscuit / AIP)** — authorize *what* a
   delegated agent may do, verifiable offline by the receiver.

## Why we need it

- The original worry — "agent A shouldn't be able to use agent B's tool/skill
  by calling as B" — is only truly enforceable **at the wire**, not with
  in-process string/signature checks. When the call crosses the network, the
  receiver (who does NOT hold A's key) must verify identity + authority.
- Today `arcteam`/NATS inter-agent messages have **no cryptographic identity**
  (OWASP ASI07) and the policy `agent_did` is asserted, not proven, once it
  leaves the originating process.
- Federal (NIST 800-53 IA/AU/AC, zero-trust 800-207) requires this for any
  multi-host deployment. Single-process/local-only deployments do **not** need
  it yet — that's why it's parked.

## What we need (the two controls)

### 1. Transport identity — NATS mTLS + NKeys
- Each agent connection authenticates with NATS NKeys (Ed25519) / mTLS.
- A rogue process can't post as `arcagent-002` without that agent's key.
- Maps to: SC-8 (transmission confidentiality), IA-3 (device id), ASI07.

### 2. Authorization — capability tokens (attenuable, offline-verifiable)
- Carry a **Biscuit** (or AIP/IETF-AAT) token in the message header.
- Token encodes: which tools, which arg constraints, TTL, and the **delegation
  chain** (A → B → C). Each hop can only *narrow* authority (monotonic
  attenuation), never widen it.
- Receiver verifies the whole chain with just the root public key — **no
  call-home**, which suits air-gapped/SCIF federal.
- This is the actual answer to "A can't act as B": B's token can't be minted or
  widened by A, and the receiver checks it.
- Maps to: AC-6 (least privilege), AU-10 (non-repudiation), ASI03.

## What we already have (don't rebuild)
- `arctrust` Ed25519 identity + DID + signed audit chain (`SignedChainSink`).
- In-process tool-dispatch: signed `ToolCall` + `IdentityLayer` (fail-closed,
  deny-by-default at ent/fed) + per-op audit. **Keep** — it's the reusable
  primitive and defeats the asserted-DID-string bug; just don't mistake it for
  the cross-host solution.
- Artifact trust at load: Sigstore/SLSA on skills/extensions.
- Untrusted/LLM-generated code: Firecracker sandbox in `arcskill`.

## Rough shape when we build it
- arctrust: add a capability-token module (start against the Rust `biscuit-auth`
  crate / a Python binding; plan migration to IETF AAT when it stabilizes).
  Define the **convention for attaching tokens to NATS headers** (AIP spec
  covers MCP/A2A/HTTP but not NATS — we define that).
- arcteam: issue a delegation token at each spawn/delegation boundary
  (one-time per delegation, NOT per call); attach to outbound messages.
- Receiver side: verify mTLS + token chain before accepting any tool/skill
  invocation request; deny-by-default.
- Tier it: personal = none needed; enterprise = mTLS + single-hop tokens;
  federal = mTLS (FIPS ciphers) + chained tokens with a hard delegation-depth
  cap + short TTLs (≤15 min).

## Explicitly out of scope / not worth it now
- Per-call signing of in-process calls (theater).
- TEE/remote attestation (TPM/SEV-SNP) — only for cloud federal enclaves where
  the host operator isn't trusted; stub the integration point, don't build.

## Pointers
- Full research + citations: this session's "Research legitimate-entity
  execution patterns" agent output (NIST NCCoE Feb-2026, MCP 2025-11 auth spec,
  AIP arxiv:2603.24775, IETF AAT draft, OWASP Agentic Top-10, SAGA).
- Memory: `project_identity_enforcement` (decided direction recorded there).
