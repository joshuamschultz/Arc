# Buzz vs Arc — Maturity, Positioning, and Strategic Threat Assessment

## (a) Maturity Scorecard

| Signal | Buzz | Arc |
|---|---|---|
| Backing | Block, Inc. (Square/Cash App parent) — dedicated release eng, internal K8s staging cluster (Terraform+ArgoCD), signed/notarized builds, mobile store pipeline, Buildkite CI | Solo/small-team, local-only, no deployed infra |
| GitHub traction | 11.7k stars, 929 forks, 243 open issues, 349 open PRs (all live) | N/A — not public |
| Release cadence | 74 tagged versions in visible CHANGELOG (back to v0.3.0), PR numbers past #2805 → thousands of merged PRs, near-weekly desktop releases, 3 independent release lanes (desktop/relay/mobile) with immutable RC tags | No release process — pre-ship |
| LOC | ~324k Rust (26 crates) + ~284k TS/TSX (desktop) ≈ 608k+ total | 118k total, core capped at 3,500 LOC by design |
| Tests | 342 Rust test files, 5,309 `#[test]`/`#[tokio::test]` functions, 117 desktop test files, dedicated E2E suite (`buzz-test-client`), a documented multi-agent E2E testing guide | 978 test files (higher file-to-LOC test density, deliberately) |
| Formal verification | **TLA+ specs** (`MultiTenantRelay.tla`, `GitOnObjectStore.tla`) + **Tamarin protocol proofs** (`MultiTenantAuth.spthy`, pairing `NIP-AB.spthy`) — machine-checked tenant isolation and auth guarantees | None found |
| Supply chain hygiene | `cargo-deny` (license allowlist + curated, justified advisory exceptions), Renovate with per-package override rules, `#![deny(unsafe_code)]` repo-wide, `cargo audit` in CI | ruff/mypy/pip-audit gates, no deny.toml equivalent |
| Governance | Contributor Covenant v2.1, standard Block OSS governance doc, conventional-commit-enforced squash merges, "no special access required" external contributor path, AI-assisted PRs explicitly welcomed (with human-review requirement) | Informal, single maintainer |
| CI discipline | `lefthook` pre-commit/pre-push mirrors CI path-filters exactly (documented drift-prevention comments), `just ci` = fmt+clippy+unit+desktop+tauri+mobile | ruff/mypy/pytest gate |

**Verdict:** Buzz is not a side project or a demo. It is a fully industrialized, corporately-funded platform with release engineering, formal verification, and real internal production usage at Block, running at roughly 5x Arc's LOC and with a live external contributor community. This is the most mature "agent-adjacent" OSS project either of us has assessed.

## (b) The Honest Scope Table (verbatim from README.md)

| ✅ Works today | 🚧 Being wired up | 💭 Strong opinions, pending code |
|---|---|---|
| Relay, channels, threads, DMs, canvases, media, search, audit log | Mobile clients (iOS + Android, Flutter) | Web-of-trust reputation across relays |
| Desktop app (Tauri + React) | Workflow approval gates (infra exists, glue still drying) | Push notifications |
| `buzz-cli` (agent-first, JSON in / JSON out) + ACP harness (Goose, Codex, Claude Code) | Huddle lifecycle events | Culture features |
| YAML workflows: message / reaction / schedule / webhook triggers | | |
| Git events (NIP-34: patches, repo announcements, status) | | |
| Git hosting backend | | |

Footnote in the README: *"Please do not plan your compliance program around the 💭 column yet."* — Buzz's own team explicitly disclaims compliance-readiness for the unshipped tier, which is a real opening for Arc but only against that tier, not the ✅ column, which is large and genuinely shipped (confirmed by 5,309 passing tests and E2E suites).

VISION.md's own status table adds nuance: approval gates are architecturally complete (schema, REST, MCP tool, UI) but the *executor* doesn't persist the token, so any run hitting `request_approval` currently fails (bug WF-08) — a concrete, named gap, not vague hand-waving.

## (c) Target User & Positioning

VISION_SOVEREIGN.md and README are explicit: **developers and OSS/self-hosted communities** who want "your domain, your relay, one thing" instead of Slack+Discord+GitHub+CI-dashboard sprawl. The "I work at Block" README section confirms **Buzz is Block's internal dogfooded Slack+GitHub+CI replacement**, now being open-sourced — this is not a toy; it's eating Block's own tooling stack. Secondary audience is hosted-multi-tenant operators (the community/tenant-isolation work, proven via TLA+, is clearly aimed at someone standing up a SaaS on top of OSS Buzz).

There is **zero mention of federal, FedRAMP, NIST, CMMC, classification, or air-gapped deployment** anywhere in the vision docs, security policy, or architecture doc. Buzz is explicitly competing with **Slack + Discord + GitHub + CI dashboards + search tools**, not with agent-governance frameworks. It is not federal-shaped, not compliance-first, and not attempting Arc's regulated-environment thesis.

## (d) Overlap Map — What Buzz Already Delivers of Arc's Pitch

| Arc pillar | Buzz equivalent | Real gap vs Arc |
|---|---|---|
| **Identity** (DID per entity) | secp256k1 keypair per human/agent, portable across "communities," NIP-05 handles | Not a DID standard; no federal identity binding; agents inherit access via NIP-OA owner attestation (conceptually similar to Arc's caller_did, genuinely well done) |
| **Sign** (verify artifacts before use) | Every *event* is Schnorr-signed (NIP-42/98) — messages, reactions, workflow steps, git pushes | No signing/attestation of the *tools themselves* — MCP servers are unsigned local processes; no Sigstore/Rekor equivalent for skills/extensions |
| **Authorize** (policy pipeline, first-DENY-wins) | "Channel membership is the **only** access control mechanism" — explicitly no ACLs, no capability taxonomy | This is the biggest real gap. No tool-level allowlist/denylist, no per-call policy evaluation, no tiered (personal/enterprise/federal) posture. Buzz's authorization model is coarse by design. |
| **Audit** | SHA-256 hash-chain audit log, explicitly "tamper-evident, not tamper-resistant" (their own words — an attacker with DB write access can recompute the chain) | Arc's SignedChainSink + classification-awareness target a stronger threat model; Buzz doesn't claim otherwise |
| **Sandboxing / RCE containment** | None. SECURITY.md states the dev-mcp shell "runs at the operator's trust level, like bash itself" | Arc's Firecracker-microVM mandate directly answers ASI05; Buzz has no answer here at all |
| **Tiered compliance** | Doesn't exist — one security posture for everyone | This is Arc's clearest differentiator |

**Bottom line:** Buzz has already built a very good identity+signing+audit substrate for a *collaboration platform*. It has **not** built a tool-call governance/policy engine, sandboxed execution, or any compliance-tiering — those are Arc's actual product, and Buzz's own SECURITY.md concedes the boundary ("the shell runs at the operator's trust level").

## (e) Blunt 12-Month Threat Assessment

If Buzz continues its current trajectory (weekly desktop releases, ~350 open PRs in flight, Block's internal engineering org behind it), in 12 months it will very plausibly have: mobile GA, working approval gates, web-of-trust reputation, and a mature git-forge layer — i.e., a complete Slack+GitHub+CI replacement with agents as first-class members, backed by a company with real money and real production load. It will **not**, on current evidence, grow a fine-grained tool-authorization engine, sandboxed code execution, or federal-compliance tiering — those aren't on its vision roadmap at all (VISION_MODERATION and VISION_ACTIVITY are about community/notification UX, not agent governance).

Arc's defensible position in 12 months is narrow but real: **the governed runtime layer that answers "may this agent call this tool right now, and can I prove it after the fact to an auditor."** That's a genuinely different product surface than Buzz's "everyone's in one room" bet. The risk isn't that Buzz replaces Arc — it's that a buyer evaluating "agent identity/audit" sees Buzz's signed-event-log and assumes it's "solved," and never gets far enough to ask the authorization-policy question Arc is built to answer. Arc must lead with the policy/sandboxing/compliance-tiering story specifically, not the generic identity/audit story, or it reads as a smaller, worse-resourced clone of something Block already ships to 11.7k GitHub stars.

## (f) Three Strategic Options

**1. Compete head-on (build a Buzz-equivalent collaboration layer).**
*For:* Arc's Four Pillars give it a legitimate identity/audit foundation; could out-execute on the authorization layer from day one instead of retrofitting it like Buzz is doing.
*Against:* Buzz has a 600k-LOC, formally-verified, Block-funded, 2+-year head start with a live community. Racing to rebuild channels/canvases/git-hosting/mobile is not a fight Arc can win on resources, and it dilutes Arc's actual differentiator.

**2. Differentiate narrowly (governed-runtime-only, no workspace UI).**
*For:* Plays to Arc's actual strength — the policy pipeline, sandboxing, and compliance tiering nothing else in this space has. Smaller surface area matches Arc's <3,500-LOC core discipline. Federal/regulated buyers are a market Buzz explicitly isn't chasing.
*Against:* Smaller market than "team chat + agents." Requires discipline to keep saying no to workspace-feature scope creep that every prospect will ask for.

**3. Integrate with Buzz (Arc as the governed runtime behind a Buzz agent seat).**
*For:* Buzz is Apache 2.0 and explicitly protocol-native (ACP via `buzz-acp`, MCP tools, Nostr events) — an Arc-run agent could sit behind a Buzz community as a first-class member, with Arc's PolicyPipeline deciding whether a tool call is allowed before the resulting signed event ever reaches the relay. This gives a federal/regulated customer Buzz's UX with Arc's compliance underneath, and rides Buzz's traction instead of fighting it.
*Against:* Cedes the workspace narrative to Block; Arc becomes a component, not a platform, in the buyer's mental model. Also depends on Buzz's authorization model staying coarse — if Block ever builds real per-tool policy (nothing in the vision docs suggests it's planned, but it's a company with resources), the integration wedge narrows.

**Recommendation implied by the evidence, not asserted:** Option 2, with Option 3 as an explicit go-to-market wedge for accounts that are already evaluating or running Buzz. Option 1 is not credible given the resource gap.
