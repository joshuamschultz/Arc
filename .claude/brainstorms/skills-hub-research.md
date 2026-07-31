# Skills Hub — Research Brief

> Web + codebase research synthesized 2026-07-10. Five parallel research streams:
> hub landscape, agentic skill loading, Hermes skill management, registry
> trust/pairing patterns, and the current arcskill state.
> Feeds: /brainstorm → /build → /specify for the Arc Skills Hub.

## 0. The one-paragraph conclusion

The ecosystem has standardized on the **agentskills.io SKILL.md format** (32
adopters: Claude Code, Hermes, OpenClaw, Codex, Gemini CLI, Copilot, Cursor…),
and on a **directory-vs-registry split** (human-browsable website as a thin
layer over an API-first, versioned registry). Nobody except NVIDIA's Verified
Agent Skills catalog cryptographically signs skills — Hermes has no signing at
all, Anthropic's model is explicitly trust-based, and ClawHub only added
scanning after 11.9% of its registry turned out to be malware (ClawHavoc).
Meanwhile, **Arc already owns a complete, tested client-side verified-install
pipeline** (`arcskill/hub/`: fetch → Sigstore verify → CRL → scan → sandboxed
dry-run → atomic activate → lockfile → audit) that is currently unwired, and a
ready-made pairing template (`arcgateway.pairing`). The gap is not a client —
it's **a hub server, a website, and three wiring fixes**. The differentiator
writes itself: *the signed, verified, federal-grade skills hub* in a market
whose incumbents are unsigned and scan post-hoc.

---

## 1. Landscape: who hosts skills today

| Hub | Model | Trust | Notable |
|---|---|---|---|
| **ClawHub** (clawhub.ai, OpenClaw) | npm-like: `clawhub publish/install/update/pin`, semver, `.clawhub/lock.json` | Digest check + VirusTotal/Gemini scan (added *after* ClawHavoc); GitHub-account-age gate (trivially defeated) | The cautionary tale (§3) |
| **Anthropic ecosystem** | No central hub: github.com/anthropics/skills + plugin marketplaces + `/v1/skills` API | Explicitly trust-based, zero platform verification | Spec owner; distribution is fragmented across products |
| **Hermes Skills Hub** (NousResearch/hermes-agent) | Five source adapters w/ trust tiers; "taps" = plain GitHub repos as publishable skill sets, zero server | "Skills Guard" content scanner; **no cryptographic signing anywhere** | 213k stars; the framework on your DGX Spark. ClawHub is one *low-trust external source* to it |
| **Official MCP registry** | API-first metadata registry consumed by sub-registries | Reverse-DNS namespacing verified via GitHub OAuth/OIDC or DNS proof | Best-in-class anti-squatting design |
| **Smithery** | Combined MCP + skills registry (smithery.ai/skills) | Curated | Closest direct precedent for a combined hub |
| **NVIDIA Verified Agent Skills** (May 2026) | Review → scan ("SkillSpector") → eval → **OMS detached signature** (`skill.oms.sig`) → SKILLCARD.yaml → catalog | Only cryptographically signed catalog found | The signed-catalog prior art; validates the Arc thesis |
| **LangSmith Context Hub** | `hub pull` to disk or virtual-FS backend; tags (dev/staging/prod) vs pinned commit hashes | Org-internal | Cleanest tag-vs-hash promotion model |
| **skillsmp.com / awesome-lists** | 2M-file GitHub scrape vs hand-curated lists | None vs human curation | Proof that **curation is the moat** — unmoderated firehoses push users to curated lists |

**Directory vs registry** is now standard vocabulary: build the human website as
a presentation layer over a machine-readable, versioned registry API. Never
conflate them.

## 2. The format: agentskills.io SKILL.md

- Layout: `skill-name/SKILL.md` + optional `scripts/`, `references/`, `assets/`.
- Frontmatter: `name` (≤64 chars, matches dir), `description` (≤1024, what+when),
  optional `license`, `compatibility`, `metadata` (free string map — where
  `author`/`version` live), `allowed-tools` (experimental and **not enforced**
  by Claude Code — never treat it as a sandbox).
- **Progressive disclosure, 3 tiers**: (1) name+description always in context
  (~100 tokens/skill), (2) SKILL.md body on activation (<5k tokens), (3) bundled
  files on demand. The agent *pulls* SKILL.md via file reads; the harness
  doesn't push it.
- Validation: `skills-ref validate`.
- Hermes adds: `platforms`, `requires_toolsets`/`fallback_for_toolsets` gating,
  `required_environment_variables`, category dirs, `lock.json` content-hash
  drift detection (`hermes skills check/update`).
- **Arc's format is a strict superset**: agentskills.io fields plus required
  `triggers`, `tools`, `version` frontmatter and 7 required sections
  (`skill_validator.py`). An Arc hub can host spec-compliant skills and carry
  Arc extensions in `metadata` — meaning **Hermes/OpenClaw/Claude Code agents
  can consume Arc-hub skills unmodified**, and Arc agents get the richer
  contract when present.

## 3. Threat model: what actually went wrong elsewhere

- **ClawHavoc (Jan–Feb 2026)**: 341 of 2,857 ClawHub skills malicious (later
  counts 1,184–1,467 across overlapping studies; treat as "double-digit % of a
  real registry"). Attack: "ClickFix 2.0" — SKILL.md "Prerequisites" instruct
  the *agent* to present a fake install dialog so the **human** runs the
  attacker's command (AMOS stealer). The agent is the social-engineering
  intermediary. Scanning (VirusTotal + Gemini Code Insight) was bolted on after.
- **Academic survey (arXiv 2602.12430, 42,447 skills)**: 26.1% contain ≥1
  vulnerability pattern; scripts carry 2.12× the risk of instruction-only
  skills. Proposes T1→T4 trust tiers with 4 gates (static, LLM semantic-intent,
  sandboxed behavioral, permission-manifest diff) + runtime reputation.
- **npm Sigstore bypass (May 2026)**: 633 malicious packages shipped with
  *valid* provenance via a compromised maintainer account. **Signing proves
  provenance, not safety** — signing and content-scanning are two separate
  gates, never one badge.
- **Hermes self-write persistence gap**: `skill_manage` lets the agent plant a
  skill that loads in future sessions — prompt injection → persistence.
  Mitigation is an optional human-approval queue. Arc already answers this with
  the sign/TOFU gate on agent-authored roots.
- Registry hygiene that works: publish-time scanning, quarantine window before
  indexing, namespace verification (MCP registry's GitHub-OIDC/DNS-proof
  model — steal this), append-only publish transparency log, install-scripts
  off by default, lockfiles.

## 4. What Arc already has (from the codebase map)

**Client — built, tested, unplugged** (`packages/arcskill/src/arcskill/hub/`,
622 tests, 90% cov):
- `sources.py`: adapters for GitHub releases, HTTP registry (`{url}/index.json`),
  **agentskills.io well-known** (`/.well-known/skills/index.json`), local.
- `verify.py` (775 LOC): Fulcio + Rekor + SLSA attestation + CRL, tier-aware
  issuer policy, load-time re-verify (`verify_artifact_at_load`) closing the
  TOCTOU gap. `UnsafeNoOp` never used.
- `installer.py`: 8-stage pipeline, atomic activate (PEP 706 `filter="data"`),
  quarantine cleanup, audit.
- `scanner.py` + `dry_run.py`: regex/AST/semgrep scan + Docker/Firecracker
  sandboxed dry-run.
- `lifecycle.py`: CRL refresh, boot revocation check, `quarantine_skill()`.
- `lock.py`: atomic `~/.arc/skills/.hub/lock.json`.

**Three wiring gaps** (all named in SPEC-033 SDD or verified by grep):
1. `[skills.hub]` config never read by arcagent; `HubTrustBackend` has zero
   importers — loader needs per-root trust-backend selection (Ed25519 for
   agent/workspace roots, Sigstore/hub for hub-sourced global root).
2. Hub installs land in `~/.arc/skills/` but the loader scans
   `~/.arc/capabilities/` — hub skills are invisible to the agent.
3. No CLI: `install()/uninstall()/update()` are Python-only; `arc skill hub …`
   doesn't exist.

**Pairing template**: `arcgateway/pairing.py` — mint 8-char code → operator
`arc gateway pair approve` → tiered Ed25519 signature over
`sha256(code + minted_at)` (federal requires, enterprise warns, personal
skips) → throttle → hashed IDs only → full audit. Adapt, don't reinvent.

**Improver**: orthogonal (mutates installed skills, doesn't fetch), but its
seams pattern (`Mutator`/`Signer`/`EvalRunner`/`ApprovalProvider`) is the model
for hub extension points. Future hook: hub-published skills carrying golden
eval suites feed the improver's `EvalGate` directly.

## 5. Patterns to adopt (with sources of truth)

1. **Discovery**: Terraform-style `/.well-known/` service document
   (`/.well-known/skillshub.json` → `{"skills.v1": ".../api/v1/"}`) — decouples
   hostname from API versioning. *Also* serve
   `/.well-known/skills/index.json` (agentskills convention) so Hermes,
   OpenClaw, and Arc's own `WellKnownSource` adapter can pull with zero client
   changes. Interop for free.
2. **Metadata vs content**: npm packument model — one mutable, ETag-cacheable
   JSON doc per skill (versions, dist-tags, scan status, signatures pointer);
   content itself immutable, digest-addressed, CDN-cacheable forever.
   "Check for updates" = conditional GET on the metadata doc.
3. **Snapshots per revision** (HuggingFace): `skills/<name>/snapshots/<digest>/`
   on the client, not one mutable dir — trivial rollback, no partial-update
   corruption. Arc's lockfile already records content hashes; align the disk
   layout.
4. **Pinning**: lockfile in the consuming agent's repo (ClawHub) + tag-vs-hash
   promotion semantics (LangSmith: tags auto-follow, hashes freeze — pin hashes
   in federal).
5. **Namespacing**: verified namespaces from day one — `@org/skill-name`,
   ownership proven via DID signature (Arc-native) and/or GitHub-OIDC/DNS proof
   (MCP registry model). Kills typosquatting structurally.
6. **Pairing**: OAuth Device Authorization Grant (RFC 8628) shape, implemented
   with the arcgateway mint-code/approve/tiered-signature machinery; then
   short-lived scope-split tokens (read/pull vs publish), npm-granular-token
   style. Agent DID = the underlying identity. GNAP and MCP-I are too immature
   to build on (noted for later federation).
7. **Transparency log**: every publish event into an append-only signed hash
   chain — `arctrust.audit.SignedChainSink` already is one. Compromised-account
   publishes become detectable minutes after the fact (OpenSSF technique).
8. **Trust tiers, not binary badges**: T1 instructions-only/unvetted → T4
   signed+scanned+eval'd, mapped onto Arc's personal/enterprise/federal
   stringency. Website shows the tier, scan verdict, and signature status as
   separate signals (the npm lesson: "signed" ≠ "safe").
9. **OCI as backbone — later, not now**: the Agent Skills OCI Artifacts spec
   (Cosign referrers, collections, `skills.lock.json`) is real prior art and a
   good federation/air-gap story (Zot/GHCR/Harbor for free), but it's
   container-shaped and still needs a search API in front. The smallest correct
   MVP is a plain registry API + object storage serving the wire formats the
   arcskill client already speaks. Revisit OCI when federation matters.

## 6. Recommended architecture (advisory)

```
packages/archub/                     # NEW — the hub server (deployable)
  api/          # registry: /.well-known discovery, index.json, packuments,
                #   digest-addressed bundle download, search
  publish/      # submit pipeline: auth → scan → quarantine → sign-verify →
                #   transparency log → index
  pairing/      # device-code pairing (adapted from arcgateway.pairing),
                #   scoped token issuance (read vs publish)
  web/          # design-forward directory: browse/trending/categories,
                #   skill detail = writeup + skill card (tier, scan, signature,
                #   provenance, versions, changelog), submit flow
arcskill/hub/   # EXISTS — client pipeline; add ArcHubSource adapter (thin:
                #   RegistrySource + auth token + attestation fetch)
arccli          # NEW surface: arc skill hub search/install/update/pin/
                #   uninstall/quarantine + arc hub pair
arcagent        # wire [skills.hub] config, per-root trust backends,
                #   add hub install root to loader scan roots
```

**Publish flow**: `arc skill hub publish` (or web form) → publisher identity
(DID signature; org namespace) → static + LLM semantic-intent scan → sandboxed
dry-run → quarantine window → transparency-log append → indexed with tier.
**Consume flow**: agent/CLI pulls metadata (ETag) → downloads immutable bundle →
existing 8-stage arcskill pipeline runs unchanged → lockfile → loader picks it
up from the hub root.
**Interop**: any agentskills-compatible agent (Hermes, OpenClaw, Claude Code)
pulls via the well-known index with no Arc code; Arc agents additionally get
signatures, attestations, CRL, and tier policy.

## 7. Phasing (dependency order, not a timeline)

1. **Wire what exists** — CLI surface, `[skills.hub]` config into arcagent,
   loader scan-root + per-root trust backend (closes SPEC-033's named gap).
   Prove end-to-end against a static well-known index on any HTTPS host.
2. **Hub server MVP** (`archub`) — well-known discovery + index + packuments +
   digest bundles + publish API with scan/quarantine/transparency log.
3. **Website** — directory layer over the registry API; writeups, skill cards,
   trust signals; submit flow.
4. **Pairing + private hubs** — device-code pairing, scoped tokens, org
   namespaces, enterprise/federal tier policies (signed allowlists, hash-pinned
   locks).
5. **Later**: OCI artifact federation, cross-hub DID trust, improver loop
   (hub-published golden suites → EvalGate → improved skills republished).

## 8. Open decisions for Josh

- **Repo/product boundary**: `archub` as a package in this monorepo (gateway-
  adapter precedent says yes) vs a separate repo/deployable product.
- **Hosting posture**: one public Arc-run hub, self-hostable hub software, or
  both (both is the federal answer — air-gapped self-host is a requirement
  there; the client's `_LocalSource` + `RegistrySource` already support it).
- **Signing root for community publishers**: DID/Ed25519-only (Arc-native,
  air-gap friendly) vs also accepting Sigstore-keyless (GitHub-CI publishers) —
  `verify.py` already handles both; the question is what the *hub* requires per
  tier.
- **Website stack**: extend arcui vs a standalone design-forward site (likely
  standalone — different audience, public marketing surface).
- **Name/brand** for the hub.

## Sources (primary)

agentskills.io/specification · github.com/anthropics/skills · Anthropic eng
blog "Equipping agents for the real world" · docs.openclaw.ai (skills, clawhub,
CLI) · Koi Security / Antiy / Snyk ToxicSkills (ClawHavoc) · arXiv 2602.12430
(42k-skill survey) · unit42.paloaltonetworks.com (OpenClaw supply chain) ·
hermes-agent.nousresearch.com/docs (skills, hub) · NVIDIA blog (Hermes on DGX
Spark; Verified Agent Skills) · registry.modelcontextprotocol.io ·
smithery.ai/skills · github.com/ThomasVitale/agents-skills-oci-artifacts-spec ·
RFC 8628 (device grant) · npm/PyPI provenance docs · OpenSSF transparency-log
monitoring · LangSmith Context Hub blog · HuggingFace hub caching docs.
