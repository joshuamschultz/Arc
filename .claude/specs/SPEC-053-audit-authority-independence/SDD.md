# SPEC-053 — SDD: Audit-authority independence

**Status:** PENDING
**Traces:** PRD REQ-001 … REQ-015.

---

## 1. Design in one paragraph

`WormSink.__init__` **already** takes a parameter named `operator_private_key`
(`arctrust/audit.py:211`) and verifies with the operator public key derived from
it. The codebase is structurally ready; the only defect is that arcagent *feeds
that parameter the agent's DID seed* at three call sites. The fix is therefore
small and mostly a **wiring change plus one new custody primitive**: introduce an
`arctrust` operator-key loader, generate the key in `arc init`, and pass it (not
`identity.signing_seed`) into all three WORM sinks. Federal adds a witness by
reusing the existing checkpoint anchor. Two policy hardenings reorder
authentication ahead of the pipeline's two short-circuits and bind the cache key
to identity.

---

## 2. Module boundaries (the load-bearing decisions)

| Package | Owns | Must NOT |
|---------|------|----------|
| **arctrust** | `OperatorKey` custody primitive; `audit` (`WormSink`, `verify_chain`, `read_verified_anchor`); the witness-anchor Protocol + implementations; `keypair` primitives. | import arcagent / arcllm / arcrun / arcteam. |
| **arcagent** | Loading the operator key read-only at startup and **wiring** it into the policy sink (`agent.py`), the improver sink (`skill_improver/_runtime.py`), and the trace checkpoint sink (`model_manager.py`). Config fields for operator-key location. | define its own operator-key type; hold the operator key in any writable/agent-tool-reachable path; keep any agent-seed WORM path. |
| **arccli** | Generating/persisting the operator key at `arc init` (deployment bootstrap). | contain audit or witness logic (delegates to arctrust). |
| **arcllm** | Unchanged. Still emits `build_checkpoint` dicts through an injected `checkpoint_sink`; never knows who signs. | import arctrust. |

**Why arctrust owns the operator key:** it already owns both `audit` and
`keypair`, and the operator key IS an audit-signing credential. Placing it in
arctrust keeps the "audited subject ≠ audit authority" boundary inside the one
package that has no agent identity of its own.

---

## 3. Components

### 3.1 `arctrust.operator.OperatorKey` (new) — REQ-001, 003, 004, 005, 006, 011, 015

A thin custody wrapper over an Ed25519 keypair, deliberately **separate from
`AgentIdentity`** so the type system prevents mixing the two authorities.

```
class OperatorKey:
    seed: bytes            # 32-byte Ed25519 seed — feeds WormSink(operator_private_key=...)
    public_key: bytes      # feeds verify_chain / read_verified_anchor

    @classmethod
    def load(cls, path, *, vault_resolver=None, vault_path="",
             generate_if_absent=False, prior_chain_exists=False) -> OperatorKey
    @classmethod
    def generate(cls) -> OperatorKey
    def save(self, path: Path) -> None      # atomic 0600 file + .pub sentinel, 0700 dir
```

- `load` reads from the vault resolver when supplied (REQ-005 / SPEC-037 seam),
  else the `0600` file; a missing file triggers `generate()` + `save()` **only**
  on a genuine first-ever bootstrap (REQ-006). Once an operator has been recorded
  (`.pub` sentinel or a prior audit chain), a missing key fails closed rather
  than regenerating — see §3.8 (REQ-016). Custody hardening (atomic bootstrap,
  `O_NOFOLLOW`, ownership/parent-dir checks) also lives in §3.8.
- Reuses `arctrust.keypair.KeyPair.from_seed` / `generate_keypair` — no new crypto.
- No `sign`/DID methods: an operator key is not an identity, it is a notary seed.

**Resolved location (default):** `~/.arc/operator/operator.key` (+ `.pub`),
i.e. under the operator config dir, **outside** any agent `workspace`
(REQ-004). Overridable via config (§3.5) and vault (REQ-005).

### 3.2 `WormSink` — REQ-001, 002, 007 (no code change, only how it is fed)

Already correct: constructor param is `operator_private_key`, and
`verify_chain` accepts the operator public key. This spec changes **callers**,
not the sink. The docstring line that says the param is a seed remains accurate;
the misleading comment lives in arcagent (`agent.py:180-181`, "The agent signs
records with its own DID seed") and is deleted with the rewire.

### 3.3 Witness anchor — REQ-009, 010 (federal)

Reuse, don't rebuild, the `e63f3a8` chain-head anchor:

- **Producer (existing):** `arcllm.trace_store.JSONLTraceStore` emits a
  `build_checkpoint(traces_dir)` dict at each rotation via its `checkpoint_sink`.
  arcagent wires that sink to `emit(AuditEvent(action="trace.checkpoint",
  extra=checkpoint), worm)` — **now operator-signed** (§3.4).
- **Reader (existing):** `arctrust.audit.read_verified_anchor(chain, operator_pubkey)`
  returns the newest verified checkpoint; `arcllm.trace_retention.verify_against_anchor`
  proves the live store still contains that head.
- **New seam — `WitnessAnchor` Protocol (arctrust):**

```
class WitnessAnchor(Protocol):
    def submit(self, checkpoint: dict[str, Any], signature: bytes) -> str: ...   # returns inclusion id/proof
    def verify_inclusion(self, checkpoint: dict[str, Any], proof: str) -> bool: ...
```

  - `TransparencyLogWitness` — Rekor-style online submitter (inclusion proof).
  - `AppendOnlyMediumWitness` — air-gapped: append the operator-signed checkpoint
    head to a second, separately-custodied WORM medium/host. **(Exact medium is
    Open Question 2.)**
  - Selected by federal config; arctrust exposes the Protocol + both impls,
    arcagent picks one. The point of an *external* witness: a holder of the
    operator key can re-sign a chain, but cannot retroactively insert the forged
    head into an append-only log they do not control. This is what closes the
    "operator key is a single point of forgery" gap the README raises.

### 3.4 arcagent wiring rewire — REQ-002, 008, 012

Three edits, each replacing the agent seed with the loaded operator key:

1. `core/agent.py:182` —
   `WormSink(self._policy_audit_log_path(), self._operator_key.seed)` and
   `build_pipeline(..., audit_sink=worm_policy_sink(worm))` unchanged otherwise.
   Load `self._operator_key = OperatorKey.load(...)` in `startup()` step 3.5
   (after identity, before tool registry). Delete the "signs with its own DID
   seed" comment.
2. `modules/skill_improver/_runtime.py` — `_build_worm_sink` takes the operator
   key instead of `_resolve_signer(identity)`'s DID seed. `_resolve_signer` for
   the *signing of mutated skills* (SPEC-033 D3) stays on the agent DID (that is
   a legitimate agent attestation); only the **audit chain** signer moves to the
   operator key. This split is the crux — call it out in the task.
3. `core/model_manager.py` — when a `checkpoint_sink` is wired, it routes through
   an operator-signed `WormSink` (federal wires the witness; lower tiers just get
   operator-signed local anchors).

`SecurityConfig` (§3.5) supplies the operator-key path; the operator key is
loaded once and shared read-only across the agent's sinks.

### 3.5 arcagent config — REQ-004, 005

Add to `SecurityConfig`:

- `operator_key_dir: str = "~/.arc/operator"` — resolved, kept outside workspace.
- `operator_vault_path: str = ""` — when set (+ vault backend configured),
  operator key resolves via the vault resolver (SPEC-037 seam).

### 3.6 arccli `arc init` — REQ-003, 015

In `commands/init.py::_init`, after writing config, generate the operator key:
`OperatorKey.generate().save(arc_dir / "operator" / "operator.key")` when absent.
Idempotent (skip if present). Personal tier: silent/zero-config. Enterprise/
federal: print the operator public-key fingerprint for out-of-band recording
(supports anti-genesis-substitution `genesis_tip` and witness bootstrap).

### 3.7 `arctrust.policy` reordering — REQ-013, 014

Single structural change in `PolicyPipeline.evaluate`: **authenticate before both
short-circuits.**

- Extract identity verification (`verify_call` / the `IdentityLayer` result) and
  run it *first*, before `_check_restricted` and before `_cache_get`. An unsigned
  or invalidly-signed call is denied immediately (Finding 6 fixed: restricted
  safe-set no longer bypasses auth).
- Cache: bind the key to the signature. `_cache_key` becomes
  `f"{agent_did}|{tool_name}|{classification}|{sig_fingerprint}|{_hash_call(call)}"`
  where `sig_fingerprint = sha256(call.signature or b"")[:16]`. An unsigned call
  and a signed call can no longer collide, and only post-identity decisions are
  cached (Finding 2 fixed: no ALLOW replay for unsigned/de-registered callers).
- No new engine feature; first-DENY-wins, fail-closed, shadow, and TTL semantics
  are otherwise unchanged.

### 3.8 Custody & witness hardening (review pass) — REQ-016..021

The base design proved the *type* separation; this pass makes the separation
*enforceable* by closing the custody and witness gaps a reviewer found reachable
now. All changes are in the same three packages; no new module.

**`OperatorKey` (arctrust) — REQ-016/017/018.** `load`/`save` are rewritten for
custody safety and gain a fail-closed bootstrap decision:

```
class OperatorKeyIntegrityError(RuntimeError): ...   # missing-after-present / symlink / mis-owned / swapped

@classmethod
def load(cls, path, *, vault_resolver=None, vault_path="",
         generate_if_absent=False, prior_chain_exists=False) -> OperatorKey
def save(self, path) -> None       # atomic exclusive publish (mkstemp + os.link), 0600, .pub sentinel
```

- **No silent regen (#3/REQ-016).** The `.pub` file written beside the key is the
  recorded-pubkey sentinel (now written at every tier via `save`). On a missing
  key file: if `.pub` exists OR `prior_chain_exists` → `OperatorKeyIntegrityError`
  (fail closed, logged); else if `generate_if_absent` → atomic bootstrap; else
  `FileNotFoundError`. On a present key, the derived pubkey is checked against
  `.pub` — a mismatch (out-of-band swap) is rejected.
- **Atomic bootstrap (#4/REQ-017).** `save` writes the full seed to a `0600`
  `mkstemp` temp then `os.link`s it into place — the final path appears
  atomically AND fully-formed; `link` raises `FileExistsError` if it already
  exists, so a racing loser adopts the winner's key instead of clobbering it.
- **Symlink/TOCTOU + no perm window (#5/#6/REQ-018).** Reads use
  `O_RDONLY|O_NOFOLLOW` and reject a symlink (ELOOP), a non-regular file, a
  non-owner file, insecure file mode, and a group/other-accessible parent dir.
  The create is `0600` in one operation (via the temp + link), so there is no
  write-then-chmod window.

**arcagent wiring — REQ-016/019/020/021.**
- `agent.startup` passes `prior_chain_exists=self._prior_audit_chains_exist()`
  (policy / trace-checkpoint / skill_improver chains) to `OperatorKey.load`, and
  calls `self._verify_witness_consistency()` after building the witness.
- `_build_witness` reads the medium from the new `security.witness_medium_path`
  (default `~/.arc/witness/anchor.log`, **outside** `operator_key_dir`) — REQ-019a.
- `_verify_witness_consistency` reads the newest local anchor
  (`read_verified_anchor(trace-checkpoint chain, operator pubkey)`) and calls
  `arctrust.verify_local_head_witnessed(local, witness, federal=…)`, which wires
  `verify_inclusion` and raises `WitnessDivergenceError` at federal on divergence
  / unavailable witness, warns otherwise (REQ-020).
- `configure_module_runtimes` no longer puts `operator_key` in the shared
  `available` dict; it injects it only for `_WORM_SINK_MODULE_NAMES`
  (`{"skill_improver"}`) — REQ-021. A code note points at SPEC-035/037 for full
  closure.

**`AppendOnlyMediumWitness` (arctrust) — REQ-019b.** `submit` opens the medium
`O_WRONLY|O_APPEND|O_CREAT|O_NOFOLLOW` (never truncate/seek/follow-symlink).
OS-level immutability (`chattr +a` / `chflags uappnd`) is documented as an
out-of-band deployment step, not applied in-process (it is not cleanly
reversible and would make the medium undeletable on macOS). New arctrust
export: `verify_local_head_witnessed`, `WitnessDivergenceError`.

**`build_checkpoint_sink` (arcagent) — REQ-020.** Gains `federal: bool`. Local
WORM anchoring stays fail-open (AU-5); the witness submit is split out and, at
federal, a failed `witness.submit` re-raises (mandatory witness) — below federal
it is swallowed. `ensure_model` passes `federal = tier == "federal"`.

---

## 4. Data flow (after change)

```
arc init ─────────► OperatorKey.generate().save(~/.arc/operator/operator.key)   [0600]
                              │
agent.startup() ──────────────┤ OperatorKey.load(dir, vault_resolver?)  (read-only)
                              ▼
   ┌──────────────── operator.seed ────────────────┐
   │                     │                          │
policy WormSink   improver WormSink        trace checkpoint WormSink
(agent.py)        (_runtime.py)            (model_manager.py)
   │                     │                          │
   └── verify_chain(operator.pubkey) ───────────────┘
                              │  (federal only)
                              ▼
             WitnessAnchor.submit(checkpoint, op_sig) ──► external append-only log
                              ▲
             verify_against_anchor(live_store, read_verified_anchor(chain, op.pub))

agent DID seed ──► ToolCall attestation ONLY (sign_call / verify_call)   [unchanged]
```

---

## 5. Security analysis

- **Insider forgery (the core threat):** the agent can no longer re-sign its own
  chain — it never holds the operator seed. Reading it read-only lets it *append*
  legitimate records only through `emit`; it cannot rewrite prior records without
  the private key it does not have write access to and (federal) without matching
  the external witness. AU-9/AU-10 restored against the audited actor.
- **Residual, honest:** a process that can *read* the operator key (any agent on
  the host, if one shared key — Open Question 1) can forge the *local* chain.
  Federal's external witness is the mitigation; personal/enterprise accept this
  residual (documented), matching the existing `trace_retention` honesty about
  between-anchor windows.
- **Cache/restricted hardenings:** close the two authentication-bypass paths from
  the SPEC-034 review; both are pure-logic reorderings with regression tests.
- **CLAUDE.md tension (acknowledged):** "Credentials never touch the filesystem."
  The `0600` on-disk operator key is an **interim** posture for personal/
  enterprise. The vault seam (REQ-005) is the compliant path; federal SHOULD use
  it. This tension is stated, not hidden.

---

## 6. Research Insights (the `/deepen` enrichment)

Audit-authority separation is a well-trodden problem in secure logging and
supply-chain transparency. Patterns that informed this design:

- **Separate-signer / notary model.** The foundational result is Schneier &
  Kelsey, *Secure Audit Logs to Support Computer Forensics* (ACM TISSEC, 1999):
  a log kept by an untrusted machine cannot be trusted against that machine
  unless entries are bound to a key the machine cannot forge past a point in
  time (forward-secure MACs / hash chaining + external anchoring). Our operator
  key + witness is the modern Ed25519 expression of exactly this: the *subject*
  must not hold the *authority*. Cross-check: NIST SP 800-92 (*Guide to Computer
  Security Log Management*) §2.3 recommends log generation and log storage/
  verification be under **separate administrative control**.

- **Write-Once-Read-Many / append-only medium options.** Beyond a `0600` file:
  (a) OS-level append-only/immutable flags (`chattr +a`, `+i`) — cheap, but the
  root/operator can clear them, so they harden against the *agent* not the
  *operator*; (b) object-lock WORM storage (S3 Object Lock / compliance mode) for
  enterprise; (c) hardware WORM / optical for SCIF air-gap. Arc already models
  the honest residual (between-anchor deletions) in `trace_retention`; the
  witness raises the floor without pretending the local file is immutable.

- **Transparency-log witnessing (Rekor / Certificate Transparency lineage).**
  Sigstore **Rekor** is an append-only Merkle transparency log providing signed
  inclusion proofs and a signed tree head (checkpoint) — the same "signed
  checkpoint" shape Arc already produces in `e63f3a8`. RFC 6962 (Certificate
  Transparency) and the C2SP `tlog-witness` / "witnessed checkpoint" spec
  formalize *third-party witnessing* of a log's signed tree head, which is
  precisely the federal guarantee we want: even the log operator (here, the
  operator-key holder) cannot present two different histories, because
  independent witnesses co-sign the head. Reusing `build_checkpoint` /
  `read_verified_anchor` as the checkpoint payload means Arc's anchor is already
  Rekor-checkpoint-shaped — the federal `TransparencyLogWitness` is a thin
  submitter, not a new format. **Air-gap caveat:** Rekor assumes reachability;
  RFC 6962-style *offline witness co-signing* (a second custodian signs the
  checkpoint on removable media) is the SCIF-compatible degradation (Open
  Question 2).

- **HSM / vault key custody.** FIPS 140-2/3 validated HSMs (and cloud KMS with
  non-exportable keys) are the federal answer to "where does the operator private
  key live" — the seed never leaves the boundary; signing is an oracle call. This
  is SPEC-037's domain; SPEC-053 only guarantees the loader seam (`vault_resolver`)
  is HSM-shaped (sign-by-reference is compatible because `WormSink` needs the
  seed today — a follow-up may move `WormSink` to a sign-callback so the seed
  never materializes; noted as future work, not required here). NIST 800-53
  IA-5(2)/SC-12/SC-17 and AU-9(3) (cryptographic protection of audit info) are
  the controlling controls.

- **Blast-radius / key-per-domain.** CT and Rekor use one log key but many
  independent witnesses; the analogue for Arc is Open Question 1 (one operator
  key per deployment vs per agent-root). The literature favors a single log
  authority + external witnessing over many co-located keys, because many keys
  multiply custody surface without removing the co-location trust problem — which
  is why the recommended default is one operator key + witness, not per-agent keys.

*Citations:* Schneier & Kelsey 1999 (ACM TISSEC 2(2)); NIST SP 800-92; NIST SP
800-53 Rev.5 AU-9/AU-9(2)/AU-9(3)/AU-10/IA-5/SC-12; RFC 6962 (Certificate
Transparency); Sigstore Rekor transparency-log design + C2SP tlog-witness /
signed-checkpoint spec; FIPS 140-3.

---

## 7. Traceability (REQ → component)

| REQ | Component(s) |
|-----|--------------|
| 001, 007 | §3.1 OperatorKey, §3.2 WormSink verification |
| 002, 008 | §3.4 arcagent rewire (3 call sites) |
| 003 | §3.6 arccli init |
| 004 | §3.1 save modes, §3.5 config path outside workspace |
| 005, 011 | §3.1 vault_resolver seam, §3.5 operator_vault_path |
| 006 | §3.1 auto-bootstrap on missing key (personal) |
| 009, 010 | §3.3 WitnessAnchor Protocol + impls |
| 012 | §3.3/§3.4 tier-conditional witness only |
| 013, 014 | §3.7 policy reordering + cache key |
| 015 | §2 module boundaries |
