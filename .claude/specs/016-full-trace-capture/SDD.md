# SDD — Full Trace Capture & Replay (Step 18)

## Design Overview

Step 18 makes ArcLLM capture the full raw request and response on every LLM call by default, and adds the compensating controls that keep raw-by-default federal-safe:

1. **Default flip** — `store_raw_bodies` becomes `True`. The existing `TelemetryModule._raw_bodies()` builder already produces `request_body` / `response_body`; we invert the gate and wire the encryption hook. No new body fields.
2. **Envelope encryption** (`_trace_crypto.py`) — federal tier encrypts bodies with AES-256-GCM under a data key wrapped by a KMS/vault-resolved key. Plaintext bodies never touch disk.
3. **Classification + retention** — a per-record `classification` tag and an age/size retention purge on the store.
4. **Replay reconstruction** (`trace_query.load_for_replay`) — returns a fully-formed `ReplayRequest`. arcllm reconstructs; **arcrun executes**.
5. **Lineage** — an optional `lineage` field persisted verbatim from a `load_model`/`invoke` kwarg. arcrun/arcagent build it; arcllm never does.
6. **Audited disable** — turning capture off emits a `config_change` `TraceRecord`.

### Architecture Fit

```
Agent calls load_model("anthropic", lineage=<built by arcrun/arcagent>)
  │
  └── Module stack (unchanged order):
      Otel → Telemetry → Audit → Security → Retry → Fallback → RateLimit → Adapter
                 │
                 ├── invoke() runs as today
                 ├── _raw_bodies(): build request_body + response_body   (default ON now)
                 ├── if encryption enabled:
                 │     _trace_crypto.seal(bodies, trace_id, timestamp) → envelope
                 │     request_body = response_body = None; encryption = envelope
                 ├── attach classification (config default) + lineage (verbatim kwarg)
                 └── trace_store.append(record)  → hash chain covers bodies + envelope

Store lifecycle (JSONLTraceStore):
      append → rotate (daily) → retention.purge (age/size)

Read / replay:
      trace_query.load_for_replay(trace_id) → decrypt if sealed → ReplayRequest
                                             (arcrun re-invokes; arcllm does NOT)
```

## Directory Map

### New Files

```
src/arcllm/
├── _trace_crypto.py            # Envelope: AES-256-GCM seal/unseal, KMS-wrapped data key
└── trace_retention.py          # Retention purge (whole-file age/size)
```

### Modified Files

```
src/arcllm/
├── trace_store.py              # TraceRecord: +classification, +encryption, +lineage;
│                               #   store: retention hook
├── trace_query.py              # +load_for_replay(trace_id) → ReplayRequest; transparent decrypt
├── modules/telemetry.py        # store_raw_bodies default True; encryption + classification +
│                               #   lineage wiring; audited-disable config_change emit
├── config.py                   # +TraceEncryptionConfig, +TraceRetentionConfig
├── config.toml                 # [modules.telemetry]: store_raw_bodies=true;
│                               #   [modules.telemetry.encryption] / [.retention] sections
├── registry.py                 # thread lineage= kwarg; resolve wrapping key via VaultResolver
├── __init__.py                 # export ReplayRequest, seal/unseal helpers as needed
└── pyproject.toml              # +[project.optional-dependencies] trace-encryption = ["cryptography>=42.0"]
```

### New Test Files

```
tests/
├── test_trace_capture.py       # default-flip, body completeness, hash-covers-bodies
├── test_trace_crypto.py        # envelope round-trip, AAD binding, missing-extra error
├── test_trace_retention.py     # purge age/size, chain validity over survivors
└── test_trace_replay.py        # load_for_replay reconstruction, decrypt, boundary (no execute)
```

## TraceRecord Schema Changes

Reusing existing `request_body` / `response_body` (D-436). New fields only:

```
class TraceRecord(BaseModel, frozen=True):
    ...existing fields (trace_id, timestamp, provider, model, request_body, response_body, ...)

    # NEW — classification-aware data handling (D-439)
    classification: str = "unclassified"

    # NEW — envelope encryption at rest (D-438). Present iff bodies are sealed;
    # when set, request_body/response_body are None on disk.
    encryption: EncryptedEnvelope | None = None

    # NEW — lineage token, persisted VERBATIM from arcrun/arcagent (D-443).
    lineage: dict[str, Any] | None = None

    # (unchanged) event_type discriminator
    event_type: Literal["llm_call","config_change","circuit_change","rotation"]


class EncryptedEnvelope(BaseModel, frozen=True):
    alg: str = "AES-256-GCM"
    wrapped_key: str          # base64 data key, wrapped by the KMS/vault key
    key_ref: str              # which wrapping key (for rotation / re-wrap)
    nonce: str                # base64 96-bit GCM nonce
    ciphertext: str           # base64 sealed {request_body, response_body}
    aad: str                  # "<trace_id>:<timestamp>" bound as GCM additional data
```

`compute_hash()` is untouched: it already digests every field except `record_hash`, so `classification`, `encryption`, and `lineage` are automatically inside the SHA-256 chain (D-437). Tamper-evidence over raw bodies is free.

## Encryption Design (`_trace_crypto.py`)

Envelope encryption. The content key never persists; only the wrapped data key rides the record.

```
seal(bodies: dict, *, trace_id: str, timestamp: str, wrapping_key: bytes,
     key_ref: str) -> EncryptedEnvelope:
    1. data_key = AESGCM.generate_key(bit_length=256)      # per-record content key (DEK)
    2. nonce = os.urandom(12)
    3. aad = f"{trace_id}:{timestamp}".encode()
    4. ct = AESGCM(data_key).encrypt(nonce, jcs.canonicalize(bodies), aad)   # GCM tag appended
    5. wrapped = aes_key_wrap(wrapping_key, data_key)       # RFC 3394 / SP 800-38F, nonce-free
    6. return EncryptedEnvelope(wrapped_key=b64(wrapped), key_ref=key_ref,
                                nonce=b64(nonce), ciphertext=b64(ct), aad=aad_str)

unseal(env: EncryptedEnvelope, *, trace_id: str, timestamp: str,
       wrapping_key: bytes) -> dict:
    1. verify env.aad == f"{trace_id}:{timestamp}"   # anti-transplant (D-448)
    2. data_key = aes_key_unwrap(wrapping_key, b64d(env.wrapped_key))
    3. return json.loads(AESGCM(data_key).decrypt(nonce, ct, aad))
```

Design notes:
- **Single shared KEK.** One vault-resolved wrapping key (KEK) wraps every record's per-record data key (DEK). There are no per-record or per-subject wrapping keys — the model is a flat single-KEK envelope. Rotation is handled by `key_ref` (below).
- `cryptography` is imported **inside** `_trace_crypto.py`, guarded — encryption-off path never imports it (NFR-4). Missing extra + encryption on → `ArcLLMConfigError("encryption enabled but arcllm[trace-encryption] not installed")`.
- **Wrapping key** is resolved by the existing `VaultResolver` (D-447): `resolve_api_key(env_var, vault_path)` returns the wrapping key material; the allowlisted `module:Class` backend + TTL cache are reused unchanged. Credentials never touch the filesystem (CLAUDE.md).
- **DEK wrap uses AES Key Wrap (RFC 3394 / NIST SP 800-38F)** — a nonce-free deterministic key-wrap primitive purpose-built for wrapping keys. It removes a class of nonce-management bugs and, unlike a second GCM call, has no per-op nonce and therefore no birthday-bound ceiling when many records are wrapped under the shared KEK ([Neil Madden, GCM & random nonces](https://neilmadden.blog/2024/05/23/galois-counter-mode-and-random-nonces/)).
- GCM provides authenticated encryption on the content body; the AAD binds ciphertext to record identity so a valid ciphertext cannot be transplanted onto another record (D-448).

### Research Insights

The single-KEK envelope pattern is confirmed standard. The design adopts the following hardening.

1. **Per-record DEK makes content-nonce reuse a non-issue — a documented strength (SC-13).** Because a fresh 256-bit DEK is generated per record (D-438) and encrypts exactly one plaintext, the catastrophic GCM nonce-reuse failure ([frereit AES-GCM](https://frereit.de/aes_gcm/), [elttam key recovery](https://www.elttam.com/blog/key-recovery-attacks-on-gcm)) cannot occur on the content key, and the 2^32-invocations-per-key GCM limit is trivially met. Under FIPS 140-3 IG C.H the GCM IV must come from the validated module's approved DRBG inside the crypto boundary; `os.urandom(12)` is acceptable only if it routes to that DRBG ([FIPS 140-3 IG](https://csrc.nist.gov/csrc/media/Projects/cryptographic-module-validation-program/documents/fips%20140-3/FIPS%20140-3%20IG.pdf), [SP 800-38D §8.2](https://nvlpubs.nist.gov/nistpubs/legacy/sp/nistspecialpublication800-38d.pdf)). Add a test asserting nonce uniqueness across N seals and 96-bit nonce length. (a) Security: strongest posture — one key, one message. (b) Scalability: no cross-record nonce coordination → shared-nothing per record.

2. **Federal tier runs against FIPS 140-3-validated OpenSSL in approved-only mode (adopted deployment control, SC-13).** Python `cryptography`'s `AESGCM` binds a **vendored, statically-linked OpenSSL** from the PyPI wheel, which is **not** running in a CMVP-validated, approved-only FIPS module — and under CMVP rules non-validated crypto is treated as *no protection at all* ([pyca/cryptography FIPS #7722](https://github.com/pyca/cryptography/issues/7722), [Chainguard approved-only](https://edu.chainguard.dev/chainguard/fips/non-approved-algorithms/)). arcllm is federal-first, so the federal tier **is deployed against** a FIPS 140-3-validated OpenSSL (3.1.2 is validated, [OpenSSL FIPS 140-3](https://openssl-library.org/post/2025-03-11-fips-140-3/)) in approved-only mode, surfaced as config plus a **startup self-check that fails closed if the loaded provider is not FIPS-approved**. Maps to **SC-13**. Without it, "encryption enabled" is a false assurance in a SCIF.

**KEK rotation maps cleanly to SC-12/SC-28.** Because only the small wrapped DEK references the KEK (`key_ref`), rotating the KEK never rewrites the log — new records use the current KEK, old records unseal via their stored `key_ref` ([GCP envelope encryption](https://docs.cloud.google.com/kms/docs/envelope-encryption)). Add a KEK-rotation test: seal under `key_ref="v1"`, rotate the resolver to `"v2"`, seal a new record, assert both still unseal.

## Retention Design (`trace_retention.py`)

Retention operates on the store's rotated files, never on live chain lines.

**Retention purge** (`purge(traces_dir, max_age_days, max_bytes)`):
- Enumerate `traces-*.jsonl` oldest-first (excluding today's live file).
- Delete any file whose date is older than `max_age_days`.
- While total dir size > `max_bytes`, delete oldest remaining rotated file.
- Emit a `rotation`/`config_change` marker so the purge itself is observable. Whole-file deletion means no chain line is ever rewritten (AU-10 preserved for surviving records; purge is a policy-driven end-of-life, not tampering).

### Research Insights

**Whole-file purge is tamper-evident-safe, but truncation/rollback still needs an external anchor (AU-9/AU-10).** A SHA-256 chain detects modification and reordering but **not** deletion of the tail or rollback of a whole file — an attacker who purges recent files leaves a chain that still self-verifies ([Crosby, tamper-evident logging](https://static.usenix.org/event/sec09/tech/full_papers/crosby.pdf), [rollback needs external anchor](https://dev.to/veritaschain/building-a-tamper-evident-audit-log-with-sha-256-hash-chains-zero-dependencies-h0b)). Nothing in the log distinguishes a policy purge from a malicious truncation. Recommend a periodic **signed checkpoint** (head `record_hash` + record count + file inventory) emitted to the existing `SignedChainSink`/Rekor anchor referenced in CLAUDE.md, so a purge is reconcilable against a signed manifest. One small checkpoint per rotation — negligible cost.

**encrypt-then-hash is load-bearing — lock it with a test.** The chain must hash the *ciphertext* (`encryption` field), never plaintext bodies. This carries two properties at once: **confidentiality** (durable bodies exist on disk only as ciphertext) and **chain-integrity** (the SHA-256 digest covers exactly what is on disk, so `verify_chain()` validates the sealed record without decrypting) — ADR-3 already does this. Add an explicit test: seal a record, assert the chain hashes the ciphertext and `verify_chain()` passes.

**Async purge vs. concurrent append is a race (scheduler-hardening learning).** Purge deletes whole rotated files while appends may be landing on today's live file. Operate purge **only on already-rotated files** (never today's live file), lock-guard purge against rotation, use `except Exception` (not `BaseException`) around IO, and bound the purge batch so a large sweep over thousands of files cannot monopolize the loop. [async-scheduler-hardening review](.claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md)

## Replay Read-Path Design (`trace_query.load_for_replay`)

```
async def load_for_replay(traces_dir, trace_id, *, wrapping_key_resolver=None) -> ReplayRequest:
    rec = await get_record(traces_dir, trace_id)          # existing single-record lookup
    if rec is None: raise ArcLLMTraceNotFound(trace_id)
    if rec.encryption is not None:
        bodies = _trace_crypto.unseal(rec.encryption, trace_id=rec.trace_id,
                                      timestamp=rec.timestamp,
                                      wrapping_key=wrapping_key_resolver(rec.encryption.key_ref))
    else:
        bodies = {"request_body": rec.request_body, "response_body": rec.response_body}
    req = bodies["request_body"]
    return ReplayRequest(
        provider=rec.provider, model=rec.model,
        messages=[Message(**m) for m in req["messages"]],
        tools=[Tool(**t) for t in req["tools"]] if req.get("tools") else None,
        options={k: v for k, v in req.items() if k not in ("messages", "tools")},
        lineage=rec.lineage,
    )


@dataclass(frozen=True)
class ReplayRequest:
    provider: str
    model: str
    messages: list[Message]
    tools: list[Tool] | None
    options: dict[str, Any]
    lineage: dict[str, Any] | None
    # NOTE: no .execute(), no .invoke(), no provider handle. Reconstruction only.
```

`ReplayRequest` is a pure data object. It exposes **no** method that re-invokes a model. Re-running the request, streaming, and diffing outputs are arcrun/tooling responsibilities (D-442, see Boundaries). A boundary test asserts `ReplayRequest` has no callable that performs I/O.

### Research Insights

The reconstruction-only boundary is correct; harden the inputs and outputs it moves.

- **Fail closed on FIPS/key errors — never plaintext-fall-back.** If the wrapping key or the FIPS provider is unavailable, `load_for_replay` raises (edge-case table already says this). Never return a partially-decrypted or empty-body request that a caller might replay as authentic. (a) Security: prevents a silent integrity downgrade on the replay path.
- **Size-cap and NFKC-normalize the persisted `lineage`/`classification` before storing (scheduler-hardening learning).** They arrive attacker-influenceable from upstream and are persisted verbatim; normalize + length-cap so a homoglyph or oversized blob cannot poison a downstream log viewer or a classification filter (LLM01). They stay *data*, never instructions. [async-scheduler-hardening review](.claude/solutions/security-issues/2026-02-16-async-scheduler-hardening-6agent-review.md)
- **Replay is a CUI read — carry classification on `ReplayRequest`.** Decrypting on `load_for_replay` re-materializes raw prompts/responses in memory; the returned object inherits the record's classification and must expose it so arcrun/arcui apply the same access controls and marking. Do not drop `classification` from `ReplayRequest`. (c) Boundary: arcllm returns the tag verbatim; arcrun/arcagent enforce access. [NIST SP 800-171 r3](https://csrc.nist.gov/pubs/sp/800/171/r3/final)

## Config Models (`config.py` additions)

```
class TraceEncryptionConfig(BaseModel):
    enabled: bool = False                 # federal preset sets True
    backend: str = ""                     # "module:Class" — reused VaultBackend allowlist
    key_ref: str = ""                     # wrapping-key path in the backend
    key_env: str = "ARCLLM_TRACE_WRAP_KEY"  # env fallback (dev/personal)
    cache_ttl_seconds: int = 300

class TraceRetentionConfig(BaseModel):
    max_age_days: int | None = None       # None = unlimited
    max_bytes: int | None = None          # None = unlimited

# [modules.telemetry] gains:
#   store_raw_bodies: bool = True         # THE FLIP (D-435)
#   classification: str = "unclassified"  # per-record default (D-439)
#   encryption: TraceEncryptionConfig
#   retention: TraceRetentionConfig
```

`config.toml`:
```toml
[modules.telemetry]
enabled = true
store_raw_bodies = true          # D-435 — full forensic capture by default
classification = "unclassified"

[modules.telemetry.encryption]
enabled = false                  # personal/enterprise plaintext; federal preset flips true
backend = ""                     # e.g. "arcvault.aws:KmsBackend"
key_ref = ""
key_env = "ARCLLM_TRACE_WRAP_KEY"
cache_ttl_seconds = 300

[modules.telemetry.retention]
max_age_days = 0                 # 0 / omitted = unlimited
max_bytes = 0
```

### Research Insights

**Tier-derived settings must flow through `TelemetryModule` *construction*, not per-call — or the audit lies about posture.** `store_raw_bodies`, `encryption.enabled`, the `classification` floor, and retention are process-lifetime-stable, tier-derived values read on every record. Per the solutions archive, if the real tier reaches the layer factory but a hardcoded default reaches the per-record context, enforcement is right while the audit trail systematically misreports the security posture — an **AU-2** compliance failure even when encryption is actually on. Set encryption/classification/retention at `TelemetryModule.__init__` from the resolved tier (mirroring T16.11) and add a regression test: federal-tier construction ⇒ records show encryption-on + classification at the tier floor (mirror `test_registry_propagates_tier_to_policy_context`). Cost: tier change requires an agent restart — acceptable and already operational reality. [tier-must-flow-through-construction](.claude/solutions/security-issues/2026-04-18-tier-must-flow-through-construction.md)

**`classification` should be a per-record watermark, not a static config default.** A single `classification="unclassified"` default silently *mismarks* every CUI-bearing prompt/response as unclassified — the opposite of what raw-by-default capture requires; NIST 800-171 mandates accurate CUI marking and flow control ([SP 800-171 r3](https://csrc.nist.gov/pubs/sp/800/171/r3/final)). Recommend: config supplies the tier **floor**; the actual per-record classification is resolvable per-call (like `lineage`, built upstream in arcrun/arcagent which see the data sources) and defaults to the floor when absent — it must never downgrade below the floor. (c) Boundary: arcllm stores the tag verbatim and enforces the floor; it does not *classify* content (no CUI/PII detection in arcllm — that is SecurityModule/upstream). The trace directory itself is a CUI enclave: chmod `0600`, dedicated path, logical segregation ([SP 800-171 r3 isolation guidance](https://csrc.nist.gov/pubs/sp/800/171/r3/final)).

## The Raw-Storage-Shape Decision (D-436)

Two candidate shapes were evaluated:

| Option | Description | Verdict |
|--------|-------------|---------|
| **A. Inline, reuse `request_body`/`response_body`** (chosen) | Store full bodies in the fields `TraceRecord` already has; encryption wraps them into the `encryption` field. | **Chosen.** One record = one atomically hashed unit. Hash chain already covers the fields (zero new integrity code). No join on read. No duplicate schema. Matches CLAUDE.md "smallest correct change, no legacy/dup." |
| B. New `request_raw`/`response_raw` fields | Add parallel fields as the owner's prompt literally named them. | **Rejected.** Duplicates existing `request_body`/`response_body`; two ways to say the same thing violates "no legacy/dup." (Flagged as a contradiction with the task prompt — see Boundaries note.) |
| C. Linked raw record keyed by `trace_id` | Metadata record + separate raw record joined on `trace_id`. | **Rejected.** Needs its own hash chain or a digest cross-reference (second integrity surface), a join on every replay read, and two-phase write atomicity. Only wins if raw bodies were rarely read — but replay reads them routinely. |

**Tradeoff accepted for A**: raw bodies inflate each JSONL line. Mitigated by (1) per-body size cap with truncation marker (FR-23), (2) encryption-at-rest wrapping large plaintext into ciphertext, and (3) retention purge bounding total disk (D-440).

## ADRs

### ADR-1 (D-435): Flip the default to full raw capture

**Context**: `TelemetryModule` defaults `store_raw_bodies=False`; traces are metadata-only. Metadata cannot answer "what was actually sent/received," blocking forensics and replay.

**Decision**: Default `store_raw_bodies=True`. Capture full request (messages, model, provider, tools, options) and full response on every call.

**Rationale**: Full forensic replay is a hard owner requirement (ASI10, LLM05, LLM09 post-hoc review). This deliberately reverses `telemetry.py`'s metadata-only default and SPEC-026 FR-4's stance. The safety objection to metadata-only (LLM02/LLM07 exfiltration) is an argument for encrypting durable bodies, not for discarding them — answered by ADR-2/ADR-3.

**Alternatives rejected**: Keep opt-in (fails the mandate); capture but truncate to N tokens (not reconstructable).

### ADR-2 (D-438, D-447, D-448): Envelope encryption at rest, vault-wrapped, AAD-bound

**Context**: Raw-by-default puts durable plaintext prompts/responses (incl. system prompts) on disk — a high-value target (LLM02/LLM07). Federal AU-9/SC-28 require protection at rest.

**Decision**: Federal tier seals bodies with AES-256-GCM under a per-record data key, itself wrapped by a KMS/vault-resolved wrapping key (reusing `VaultResolver`). GCM AAD binds ciphertext to `trace_id`+`timestamp`.

**Rationale**: Authenticated encryption + envelope pattern is standard for at-rest audit protection. Reusing `VaultResolver` avoids a second key-loading path and keeps credentials off the filesystem. AAD prevents ciphertext transplant. Optional `arcllm[trace-encryption]` keeps core deps clean.

**Alternatives rejected**: Full-file encryption (breaks per-record replay); storing the data key unwrapped (defeats the purpose); a bespoke key loader (duplicates vault.py).

### ADR-3 (D-437): Reuse the existing hash chain for body integrity

**Context**: Raw bodies must be tamper-evident (AU-10). We could add a body-specific digest.

**Decision**: Do nothing new. `compute_hash()` already excludes only `record_hash`, so bodies + envelope are inside the JCS SHA-256 digest and the daily-rotated chain.

**Rationale**: Smallest correct change. One integrity mechanism, already tested by `verify_chain()`.

**Alternatives rejected**: Separate body-hash field (redundant); signing bodies independently (Spec 012 signing surface, orthogonal).

### ADR-4 (D-440): Retention purge reconciles lifecycle with append-only

**Context**: Raw-by-default grows unbounded (AU-11, SI-12); lifecycle management must not conflict with an append-only tamper-evident chain.

**Decision**: Retention purges whole rotated files (age/size), never live lines.

**Rationale**: Whole-file purge is end-of-life policy, not tampering — surviving records keep an intact chain and `verify_chain()` still passes.

**Alternatives rejected**: Rewriting lines to delete bodies (breaks the chain for everyone); never purging (fails AU-11 retention).

### ADR-5 (D-442, D-443): Replay reconstruction in arcllm, execution + lineage construction out

**Context**: Replay needs a byte-exact request; lineage needs provenance arcllm cannot see.

**Decision**: arcllm provides `load_for_replay` → `ReplayRequest` (data only) and persists `lineage` verbatim from a kwarg. Re-invocation/diffing and lineage building live in arcrun/arcagent.

**Rationale**: Separation of concerns — arcllm = LLM calls + records; arcrun = loop/execution; arcagent = prompt assembly/RAG. Putting re-invoke or lineage synthesis in arcllm would import loop/orchestration concerns into the wrong package.

**Alternatives rejected**: `ReplayRequest.execute()` in arcllm (crosses the boundary); arcllm deriving lineage from messages (it has no template/RAG visibility — would fabricate).

### ADR-6 (D-444): Disabling capture is an audited event

**Context**: Secure/observable-by-default. If capture can be silently turned off, the forensic trail can be blinded without a trace.

**Decision**: `store_raw_bodies=false` emits a `config_change` `TraceRecord` (from→to, resolver identity, timestamp) before capture is downgraded.

**Rationale**: The audit configuration is itself an auditable event (AU-2). Turning ON is the default; turning OFF is the deliberate, logged act.

**Alternatives rejected**: Silent honoring of the flag (blinds the trail); refusing to allow disable (too rigid for non-federal tiers).

## Edge Cases

| Case | Handling |
|------|----------|
| Huge payload (100k-token context) | Per-body size cap (FR-23): truncate with `{"truncated": true, "original_bytes": N}` marker; encryption still seals the truncated body. |
| Streaming response | `response_body` is the reassembled final content + tool_calls + stop_reason (as `_raw_bodies` already builds post-stream); no per-chunk records. |
| Encryption key rotation | Envelope stores `key_ref`; unseal resolves the key that wrapped *that* record. New records use the current key. Re-wrap is an operational job; old records remain readable via their `key_ref`. |
| Partial write / crash mid-append | Existing `JSONLTraceStore` append is lock-guarded, single line, fsync-on-close; a torn last line is skipped by the reverse reader (malformed JSON) and re-anchored on next warm start — unchanged behavior. |
| Encryption enabled, extra not installed | `ArcLLMConfigError("encryption enabled but arcllm[trace-encryption] not installed")` at module construction (fail-closed). |
| Wrapping key unresolvable (vault + env both miss) | Fail-closed: raise `ArcLLMConfigError`; do NOT silently fall back to plaintext when `encryption.enabled` is true. |
| `load_for_replay` on encrypted record without key resolver | Raise `ArcLLMConfigError("trace is encrypted; wrapping_key_resolver required")`. |
| `lineage` kwarg is huge / attacker-supplied | Persisted verbatim but size-capped like bodies; it is data, never interpreted as instructions (LLM01). |
| Body contains PII | Out of scope for capture; `SecurityModule` (Spec 012) redaction runs earlier in the stack if enabled. Encryption is the at-rest control. |
| `verify_chain()` over encrypted records | Passes — hash covers the `encryption` field's ciphertext; no decryption needed to verify integrity. |
| Existing traces written with metadata-only (old default) | Read fine — `request_body`/`response_body` are `None`; `load_for_replay` returns a request with empty bodies and raises a clear "not reconstructable (captured metadata-only)" error. |

### Research Insights

**Raw-by-default + encryption is a real disk-growth and write-amplification ceiling at 1000s of agents.** Each line now carries full ciphertext of potentially 100k-token contexts. (b) Scalability: with AES-NI the encryption CPU cost is small, but disk I/O and growth dominate — so the size cap (FR-23) and retention purge (D-440) are not optional at federal scale, they are the throttle. Two research-backed knobs for very large bodies: (1) **compress-then-encrypt** (order matters — compress first) to cut disk, accepting that compression ratio leaks coarse length (no live-channel adaptive threat here, so acceptable); (2) for bodies over an N-MB threshold, spill ciphertext to a content-addressed blob and keep digest + `key_ref` in the line — this is the rejected Option C, but its join cost is paid only on the rare huge body, not every read. Owner may keep inline-only and rely on the cap; flag as a config knob. (c) Boundary: pure arcllm storage. [Data encryption at-rest guide](https://www.decryptiondigest.com/blog/data-encryption-at-rest-in-transit-guide)

**AES-GCM is not key-committing.** A decryptor holding multiple candidate keys is vulnerable to partitioning-oracle attacks ([key-commitment paper](https://eprint.iacr.org/2020/1456.pdf)). Not our threat model — the envelope uses a single shared KEK with one at-rest decryptor — but record it so any future multi-key scheme does not silently inherit the single-key assumption.

**Crash-safe append is correctly shaped — add a crash test.** Lock-guarded single-line fsync-append with reverse-reader skip of a torn last line matches tamper-evident-log best practice. Add a test: truncate the final line mid-write, assert warm-start skips it, re-anchors, and `verify_chain()` passes over survivors.

## Boundaries — arcrun / arcagent own these

Explicit cross-module contract. arcllm does NOT implement any of the following:

- **Replay EXECUTION** (D-442): re-invoking the model with a `ReplayRequest`, streaming the re-run, diffing/scoring old vs new output, replay orchestration and reporting. arcllm hands back a data object; **arcrun** drives it.
- **Lineage CONSTRUCTION** (D-443): computing template source, RAG document sources, variable substitution, prompt-assembly provenance. **arcrun/arcagent** build the `lineage` dict and pass it via `load_model`/`invoke`; arcllm persists it byte-for-byte and reads it back unchanged.
- **UI rendering** of raw/decrypted bodies: an arcstore/arcui concern.

## Real-Code Contradiction Note

The task prompt names new `request_raw` / `response_raw` fields. `TraceRecord` **already** has nullable `request_body` / `response_body`, and `TelemetryModule._raw_bodies()` already populates them behind `store_raw_bodies`. Per CLAUDE.md ("no legacy/dup, smallest correct change"), this spec **reuses those fields** rather than adding parallel ones (D-436). The feature is therefore a *default flip + encryption/classification/retention/replay/lineage layer*, not a new-body-field addition. The "metadata-only by default" line being overridden lives in `telemetry.py` (code + SPEC-026 FR-4 reference), not verbatim in `arcllm/CLAUDE.md`.

## Test Strategy

### `test_trace_capture.py` — ~10 tests
- Default `store_raw_bodies=True` (no config) → bodies populated.
- `request_body` completeness: messages, model, provider, tools, options.
- `response_body`: content, tool_calls, stop_reason.
- Hash changes when a body changes; `verify_chain()` passes with bodies.
- Size cap truncation marker.
- Audited disable emits `config_change` record.

### `test_trace_crypto.py` — ~10 tests
- Seal→unseal round-trip equals original bodies.
- AAD binding: altered `trace_id`/`timestamp` → decrypt raises.
- Wrapped-key unwrap via mock `VaultBackend`.
- Encryption on, extra missing → `ArcLLMConfigError`.
- Encryption on, wrapping key unresolvable → fail-closed.
- On-disk record: bodies `None`, `encryption` present.

### `test_trace_retention.py` — ~6 tests
- Purge by `max_age_days` (aged files deleted, live kept).
- Purge by `max_bytes` (oldest-first until under cap).
- Whole-file purge preserves `verify_chain()` over survivors.
- Current-day live file never purged.
- Purge vs concurrent append: guarded, live file untouched.

### `test_trace_replay.py` — ~10 tests
- `load_for_replay` reconstructs request equal to original inputs (plaintext).
- `load_for_replay` decrypts encrypted record transparently.
- Missing key resolver on encrypted record → error.
- Metadata-only legacy record → clear "not reconstructable" error.
- `lineage` round-trips verbatim.
- Boundary: `ReplayRequest` exposes no execute/invoke I/O method.

### Integration (`test_registry.py` additions) — ~6 tests
- `load_model(lineage=...)` threads lineage into the record.
- Tier presets resolve to correct capture/encryption/retention config.
- Full stack: capture happens with existing module ordering, no regressions.
