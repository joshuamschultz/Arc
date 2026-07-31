# SPEC-053 — PRD: Audit-authority independence

**Status:** PENDING
**Steering:** `.claude/steering/product.md` (federal-first agentic harness), `roadmap.md`
**Threat coverage:** ASI03 (identity/privilege abuse), ASI06 (memory/context poisoning — audit-record forgery), ASI10 (rogue agents self-covering), LLM02 (sensitive disclosure via forged audit). Compliance: NIST 800-53 **AU-9** (protection of audit information), **AU-9(2)** (store on separate system), **AU-10** (non-repudiation), **AU-9(3)** (cryptographic protection), IA-5 (key management).

Each requirement uses EARS phrasing, carries a MoSCoW priority, and states one acceptance criterion tied to a principled-coder pillar (Simplicity / Modularity / Security / Scalability).

---

## Problem statement

The WORM audit chains are signed with the agent's own DID seed
(`agent.py:182`, `skill_improver/_runtime.py:139`, and the trace checkpoint
sink). The audited subject therefore holds the audit authority: an actor with
the DID seed can recompute, re-sign, and re-seq the entire chain, and
`verify_chain()` still passes. AU-9/AU-10 give **zero** protection against the
insider the audit is designed to catch. This spec separates the two authorities.

## Goals

- Sign every WORM chain with a **deployment operator key that no agent DID ever holds**.
- Keep the agent DID key scoped to `ToolCall` attestation only.
- Make operator-key custody an "easy button" at personal tier (auto-generated,
  zero-config) and vault/witness-hardened at federal tier — same code path,
  different stringency (ADR-019).
- Add an external append-only **witness** at federal tier, reusing the existing
  `e63f3a8` checkpoint-anchor mechanism (not a parallel one).
- Fold in the two policy hardenings (cache-key identity binding; authenticate
  before restricted-mode allow) from the SPEC-034 review.

## Non-Goals

- HSM / KMS custody internals (**SPEC-037** vault-backed signing key; referenced, not built here).
- The provider budget accounting (SPEC-038), isolation backend (SPEC-036), or load-time verify (SPEC-033) — untouched.
- Any change to the `AuditEvent` schema, hash-chain format, or `verify_chain` link logic beyond adding the operator public key as the verification key and the optional witness check.
- Retroactive re-signing of already-written chains (see Migration note in SDD; local-only repo, chains are dev artifacts).

---

## Requirements

### Operator-key separation (core, every tier)

**REQ-001 (Must)** — WHERE any WORM audit chain (policy, skill-improver, or trace/checkpoint) is written, THE system SHALL sign each record with a deployment **operator key** that is distinct from every agent DID signing key.
- *Acceptance (Security):* a test signs a chain with the operator key, then asserts the agent DID public key does **not** verify the chain and the operator public key **does**; grep confirms no WORM sink is constructed from `identity.signing_seed`.

**REQ-002 (Must)** — THE agent DID signing key SHALL be used ONLY to attest `ToolCall`s; the agent process SHALL NOT pass its DID seed to any `WormSink`.
- *Acceptance (Security):* an import/callsite test asserts every `WormSink(...)` construction receives the operator key, never `self._identity.signing_seed`. Verified by unit test + code review.

**REQ-003 (Must)** — WHEN an operator runs `arc init`, THE CLI SHALL generate a fresh operator Ed25519 keypair (if none exists) and persist it operator-side as the deployment audit authority.
- *Acceptance (Simplicity):* `arc init` on a clean home produces exactly one operator key pair at a well-known path with no additional prompts at personal tier. Verified by a CLI smoke test asserting the key files exist with correct modes.

**REQ-004 (Must)** — THE operator key SHALL be stored outside the agent workspace tool-sandbox, with the private key file at mode `0600` and its directory at `0700`, and THE agent process SHALL load it **read-only**; agent-invoked file tools SHALL NOT be able to write or replace it.
- *Acceptance (Security):* a test placing the operator key at its resolved path asserts `0600`/`0700`, and asserts the path is outside `workspace` (the tool-confined root). Verified by a permissions + path-boundary test.

**REQ-005 (Should)** — WHERE a vault backend is configured, THE system SHALL resolve the operator key from the vault via the SPEC-037 seam instead of the on-disk file; WHERE no vault is configured, THE system SHALL fall back to the `0600` on-disk key.
- *Acceptance (Modularity):* the operator-key loader accepts an injected vault resolver and, when present, reads the key through it; when absent, reads the file. The on-disk path is documented as an **interim** posture (CLAUDE.md: "credentials never touch the filesystem"). Verified by a loader test with a fake resolver and a file-only fallback test.

**REQ-006 (Must)** — WHERE the deployment is personal/single-user tier, THE operator key SHALL be auto-generated and require zero configuration to function.
- *Acceptance (Simplicity):* an agent started at personal tier with no operator config still produces an operator-signed chain (key auto-created on first use if `arc init` was skipped). Verified by an end-to-end personal-tier test.

**REQ-007 (Must)** — WHEN a WORM chain is verified (`verify_chain` / `read_verified_anchor`), THE verification SHALL use the **operator public key**, and a chain signed by any agent DID key SHALL fail verification.
- *Acceptance (Security):* `verify_chain(path, operator_pubkey)` returns `True`; `verify_chain(path, agent_pubkey)` returns `False`. Verified by a unit test.

### Migration / rewire (replace, no compat shim)

**REQ-008 (Must)** — THE existing SPEC-034 policy sink, SPEC-033 skill-improver sink, and trace/checkpoint sink SHALL be rewired to the operator key, and the old agent-DID-seed wiring SHALL be **deleted in the same change** (local-only repo — no migration helper, no compatibility flag, per repo rule).
- *Acceptance (Modularity):* after the change, all three call sites pass the operator key; no branch, config flag, or fallback selects the old agent-seed path. Verified by grep (`signing_seed` absent from WORM construction) + the three sinks' tests passing under the operator key.

### Federal witness anchor (federal tier adds)

**REQ-009 (Must, federal)** — WHERE tier is federal, THE system SHALL anchor each WORM chain's head to an **external append-only witness** by reusing the existing signed-checkpoint anchor mechanism (`build_checkpoint` → operator-signed `AuditEvent(action="trace.checkpoint")` → `read_verified_anchor` / `verify_against_anchor`), so a rollback past the last anchor is detectable even by a holder of the operator key.
- *Acceptance (Security):* with a federal witness wired, truncating/re-signing a chain past its last anchor is caught by `verify_against_anchor` against the externally-held checkpoint head; the check reuses the `e63f3a8` functions, adding no parallel anchor format. Verified by a rollback-detection test.

**REQ-010 (Should, federal)** — THE witness submission SHALL follow a transparency-log / notary model (Rekor-style inclusion proof) where network egress exists, and SHALL degrade to a documented air-gapped witness medium where it does not.
- *Acceptance (Scalability):* the witness interface is a Protocol with at least one online (transparency-log) implementation and one offline/air-gapped implementation, selected by config; neither is hard-wired into `arctrust.audit`. Verified by a Protocol-conformance test with both implementations. **(Air-gapped medium is Open Question 2 — flagged.)**

**REQ-011 (Could)** — THE operator key custody MAY be delegated to an HSM/KMS at federal tier via the SPEC-037 vault seam; this spec SHALL leave the seam open but SHALL NOT implement HSM integration.
- *Acceptance (Modularity):* the operator-key loader's vault-resolver seam (REQ-005) is sufficient for SPEC-037 to supply an HSM-backed key with no change to arctrust's audit code. Verified by seam review.

### Tier stringency invariant

**REQ-012 (Must)** — THE operator-key separation SHALL apply at every tier; federal SHALL differ only by *adding* the witness (REQ-009) and vault/HSM custody (REQ-005/011) — tier SHALL remain stringency metadata, not a gate that disables separation (ADR-019).
- *Acceptance (Modularity):* personal, enterprise, and federal pipelines all produce operator-signed (non-agent-signed) chains; only the witness/vault additions are tier-conditional. Verified by a tier-parametrized test.

### Folded policy hardenings (SPEC-034 review, Findings 2 + 6)

**REQ-013 (Must)** — WHEN the `PolicyPipeline` consults its decision cache, THE cache key SHALL incorporate the call's signature/identity so that an unsigned or differently-signed `ToolCall` can never receive a cache-hit ALLOW produced for a validly-signed call, AND a cached decision SHALL never be served to a call that has not itself passed identity verification.
- *Acceptance (Security):* Finding 2 — after an ALLOW is cached for a signed call, (a) an unsigned call with identical `(tool_name, arguments, agent_did, classification)` gets no cache hit and is denied by IdentityLayer, and (b) a de-registered agent's replayed call is denied within the TTL window. Verified by two regression unit tests.

**REQ-014 (Must)** — WHEN the pipeline evaluates a call, THE `IdentityLayer` (or `verify_call`) SHALL run **before** the restricted-mode safe-set short-circuit, so an unsigned call to a safe-set tool is authenticated (and denied if unsigned) even in the degraded/offline posture.
- *Acceptance (Security):* Finding 6 — in restricted mode, an **unsigned** call to a safe-set tool returns DENY (`identity.unsigned_or_invalid`), not ALLOW; a validly-signed safe-set call still ALLOWs. Verified by a restricted-mode regression test.

### Concern-boundary invariant

**REQ-015 (Must)** — THE operator-key primitive and the witness-anchor logic SHALL live in **arctrust** (which owns `audit`, `keypair`, identity primitives); **arcagent** SHALL only wire the operator key into its WORM sinks; **arccli** SHALL only generate/load it at init. arctrust SHALL NOT import arcagent / arcllm / arcrun / arcteam.
- *Acceptance (Modularity):* an import-boundary test asserts arctrust imports none of the four packages; the operator-key type is defined in arctrust and consumed (not redefined) by arcagent and arccli. Verified by import-boundary test + review.

### Custody & witness hardening (SPEC-053 review — reachable-now defects)

**REQ-016 (Must)** — THE operator-key loader SHALL NOT silently regenerate a missing key once an operator has been recorded. WHEN the key file is absent AND a prior operator is evidenced (a recorded public-key sentinel exists, OR an audit chain exists), THE loader SHALL fail closed and alert, never regenerate; only a genuine first-ever bootstrap (no key, no sentinel, no chain) MAY generate. THE recorded public-key sentinel SHALL be written at **every** tier (personal included) so the bootstrap check has a reference to compare against. (Closes #3 — covert erasure via `rm operator.key` + restart; AU-9 audit repudiation.)
- *Acceptance (Security):* after a key is created, deleting only the key file and reloading with `generate_if_absent=True` raises `OperatorKeyIntegrityError`; a first-ever bootstrap still generates. A swapped key file (bytes replaced out-of-band) is rejected against the recorded pubkey. Verified by regen-fails-closed, prior-chain-fails-closed, first-bootstrap, and key-swap tests.

**REQ-017 (Must)** — THE operator-key bootstrap SHALL be atomic: concurrent bootstrappers (agent + CLI) SHALL converge on exactly one key, never a split chain. (Closes #4 — check-then-act race.)
- *Acceptance (Security):* N concurrent bootstrappers on one path yield a single key; the losers adopt the winner's key. Verified by a threaded atomic-bootstrap test.

**REQ-018 (Must)** — THE operator-key read and create SHALL be symlink/TOCTOU-safe and leave no permission window: opens SHALL use `O_NOFOLLOW`, the create SHALL be exclusive at mode `0600` in one operation (no write-then-chmod), and load SHALL reject a symlinked path, a non-regular / non-owner key file, and a group/other-accessible parent directory. (Closes #5 symlink capture + #6 permission window / missing checks.)
- *Acceptance (Security):* a symlinked key path is rejected; a group-accessible operator dir is rejected; a re-save over an existing key raises rather than clobbering; the created key is `0600` with no intermediate world-readable state. Verified by symlink, insecure-parent, and atomic-create tests.

**REQ-019 (Must, federal)** — THE external witness medium SHALL be separately configurable and default OUTSIDE `operator_key_dir` (the operator-key holder MUST NOT also own the witness, or the rollback check is illusory); THE medium SHALL be written append-only at the boundary (`O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW`, never truncate/seek), with OS-level immutability (`chattr +a` / `chflags`) documented as an out-of-band deployment-provisioning step (like the vault seam). (Closes #2a + #2b.)
- *Acceptance (Security):* the resolved witness medium is not under `operator_key_dir`; a symlinked medium is not followed. Verified by a medium-location test and a symlink-rejection test.

**REQ-020 (Must, federal)** — THE startup path SHALL verify the newest local operator-signed head is attested by the external witness (wiring `verify_inclusion`), and SHALL fail closed at federal on divergence or on a missing/unavailable witness; other tiers warn. Federal witness submission SHALL be mandatory — a failed submit at federal SHALL fail the operation (no fail-open swallow); local WORM anchoring remains fail-open (AU-5). (Closes #2c + #2d — `verify_inclusion` previously had zero production callers and witness submission sat in a fail-open swallow.)
- *Acceptance (Security):* at federal, a local head absent from the witness (rollback + re-anchor) raises `WitnessDivergenceError` at startup; a fresh deployment with nothing anchored does not; a failed federal `witness.submit` raises. Verified by divergence, no-false-positive, and mandatory-submit tests.

**REQ-021 (Must)** — THE operator seed SHALL NOT be broadcast to the general module `available` dict; only modules that construct a WORM audit sink (currently `skill_improver`) SHALL receive it. THE code SHALL note that full closure of in-process seed exposure requires SPEC-035 (confine bash + subprocess/file transports away from `~/.arc/operator/**` and `.audit/**`) and SPEC-037 (out-of-process/vault signing) — out of scope here. (Partially closes #1 — shrinks, does not eliminate, in-process attack surface.)
- *Acceptance (Security):* a generic (non-allowlisted) module that declares an `operator_key` parameter does not receive the seed; `skill_improver` does. Verified by two module-wiring tests.

---

## MoSCoW rollup

| Priority | Requirements |
|----------|--------------|
| **Must** | 001, 002, 003, 004, 006, 007, 008, 009 (federal), 012, 013, 014, 015, 016, 017, 018, 019 (federal), 020 (federal), 021 |
| **Should** | 005, 010 (federal) |
| **Could** | 011 |
| **Won't (this spec)** | HSM integration internals, chain re-signing/back-migration, `AuditEvent` schema changes, full in-process seed confinement (SPEC-035/037), OS-level witness immutability in-process |

## Traceability

Every REQ maps to a component in [SDD.md](SDD.md) §Components and to at least one
task in [PLAN.md](PLAN.md); the PLAN's traceability table closes REQ → component → task.
