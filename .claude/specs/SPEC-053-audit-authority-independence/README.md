# SPEC-053 — Audit-authority independence

**Status:** PENDING
**Owner:** Josh (product owner)
**Depends on / references:** SPEC-033 (skill-improver WORM chain), SPEC-034 (policy WORM chain), SPEC-037 (vault-backed signing key), commit `e63f3a8` (signed checkpoint anchor), ADR-019 (tier = stringency, not gates).

---

## Why this spec exists

Every Arc WORM audit chain is signed with the **agent's own DID seed** — the
same key the agent uses to attest its own tool calls:

```python
# arcagent/core/agent.py:182
worm = WormSink(self._policy_audit_log_path(), self._identity.signing_seed)
```

So the **audited subject is also the audit authority**. An agent (or anything
that reaches its seed) can rewrite its entire tamper-evident history —
recompute the hash chain, re-sign every record with the same key, keep `seq`
contiguous — and `verify_chain()` still returns `True`. This defeats NIST
AU-9/AU-10 non-repudiation against the *exact actor the audit exists to catch*.

It affects **all three WORM chains**:

| Chain | Call site | Current signer |
|-------|-----------|----------------|
| Policy decisions (SPEC-034) | `arcagent/core/agent.py:182` | agent DID seed |
| Skill-improver (SPEC-033) | `arcagent/modules/skill_improver/_runtime.py:139` | agent DID seed |
| Trace / checkpoint anchor (`e63f3a8`) | `arcagent/core/model_manager.py` (checkpoint_sink) | agent DID seed (when wired) |

## The fix (product-owner approved, tiered)

Introduce a **deployment/operator Ed25519 keypair, distinct from any agent DID**:

- The **operator key signs every WORM chain** (policy, improver, trace). The
  agent DID key signs **only** its `ToolCall` attestations. The agent process
  loads the operator key **read-only** and never holds write access to it.
- **Personal / enterprise:** operator key generated at `arc init`, stored
  operator-side (`~/.arc/operator/`, `0600`), auto-generated and zero-config for
  the single-user "easy button". Vault-backed **when configured** (SPEC-037 seam).
- **Federal (adds):** anchor the WORM chain head to an **external append-only
  witness**, reusing Arc's existing signed-checkpoint anchor (`e63f3a8`:
  `build_checkpoint` / `read_verified_anchor` / `verify_against_anchor`) — so
  even a holder of the operator key cannot rewrite history undetectably.
- Tier is **stringency, not a gate** (ADR-019): operator-key separation applies
  at every tier; federal *adds* the witness.

## Also folded in (SPEC-034 security review, same product-owner approval)

- **Finding 2** — the policy decision-cache key omits `signature`/`public_key`,
  so a cached ALLOW replays for ~30 s after an agent is de-registered, and a
  relayed unsigned/external `ToolCall` can get a cache-hit ALLOW that skips
  `IdentityLayer`.
- **Finding 6** — restricted-mode safe-set ALLOW runs *before* `IdentityLayer`,
  so in the degraded/offline posture an **unsigned** call to a safe-set tool
  skips authentication.

Both collapse to one structural fix: **authenticate before you short-circuit** —
run identity verification ahead of both the restricted-mode check and the cache
lookup, and bind the cache key to the signature.

## Documents

| Doc | Purpose |
|-----|---------|
| [PRD.md](PRD.md) | Requirements — EARS, MoSCoW, pillar-tied acceptance criteria |
| [SDD.md](SDD.md) | Components, module boundaries, Research Insights |
| [PLAN.md](PLAN.md) | TDD task breakdown, REQ→component→task traceability |

## Concern boundaries (one-line summary)

- **arctrust** owns the operator-key primitive, `audit`, `keypair`, and the
  witness-anchor logic. It must not import arcagent / arcllm / arcrun / arcteam.
- **arcagent** *wires* the operator key into its WORM sinks (policy, improver,
  trace) and loads it read-only.
- **arccli** *generates / loads* the operator key at `arc init`.

## Open questions for the product owner

1. **One operator key per deployment, or per agent-root?** A single shared
   operator authority is simplest and matches "deployment/operator key," but any
   agent process that can read it can forge *all* co-located chains (federal
   witness is the backstop). Per-agent-root keys shrink blast radius but multiply
   custody. Recommendation: one per deployment; confirm.
2. **Air-gapped federal witness medium.** Rekor/transparency-log witnessing
   assumes network reachability. In a SCIF with no egress, what is the witness?
   (Local WORM hardware, a second append-only host, notarized offline media?)
3. **Operator-key rotation.** `verify_chain` verifies against a single operator
   pubkey. Rotating the operator key must not invalidate chains signed by the
   prior key. Do we add a key-id/epoch per record + an operator-pubkey keyring?
   (Proposed as a Should in the PRD; confirm priority.)
