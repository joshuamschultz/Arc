# Arc Trust Model — how a capability becomes allowed or blocked

This documents the post-refactor trust/capability pipeline: the exact gates a
capability (an agent-authored tool `.py` or a skill `SKILL.md`) passes through
before it can run, and how the three deployment tiers change those gates.

It is written for an engineer or operator who needs to reason about *why* a
given capability loaded or was refused. Every claim here is grounded in current
source; symbols are named so you can jump to them.

- Trust primitives (signing, TOFU, approval store, tiers): `arctrust`
- The load pipeline that consults them: `arcagent.capabilities.capability_loader`
- The AST import gate: `arcagent.tools._dynamic_loader`
- Operator surfaces: `arccli.commands.trust`, `arcui.routes.trust`

> **One sentence:** an artifact loads only if it survives the AST import gate,
> then (above personal tier) carries a valid signature, then is approved by the
> per-tier TOFU gate. A signature proves *who wrote it and that it is unchanged*
> — never that it is *authorized*. Authorization is a human operator's decision,
> recorded as a pinned hash.

---

## 1. The three tiers

Tier is **stringency metadata, not a separate code path** (ADR-019). The same
gates run at every tier; the tier only tightens each one. The tier enum lives in
`arcagent.core.tier.Tier` (`personal` / `enterprise` / `federal`); `arctrust`
takes the tier as a plain string so the trust foundation stays independent of
arcagent's enum.

Two independent knobs are tier-resolved:

1. **Which imports** an agent-authored tool may use (`ImportPolicy`, the AST
   gate).
2. **What source approval** is required to load it (`TofuLayer`, plus the
   `require_signature` floor).

### Import policy per tier

Resolved by `resolve_workspace_import_policy(tier, allow_all_imports, allow_imports)`
in `_dynamic_loader.py`. Three modes (`ImportMode`):

| Tier | Mode | What passes | Operator override |
|---|---|---|---|
| **personal** | `ALLOW_ALL` | every import | — |
| **enterprise** | `BLOCKLIST` | everything *except* four privileged groups | `allow_imports` subtracts exceptions; `allow_all_imports=True` is a blanket opt-out to allow-all |
| **federal** | `ALLOWLIST` | deny-by-default: only the seed set + `allow_imports` | `allow_all_imports` is **ignored** — no blanket relaxation |

The enterprise blocklist (`_ENTERPRISE_BLOCKED_GROUPS`), grouped for
self-documenting errors:

- **filesystem** — `os`, `shutil`, `pathlib`, `tempfile`, `glob`
- **process/exec** — `subprocess`, `multiprocessing`
- **interpreter** — `sys`, `ctypes`, `importlib`, `pickle`, `marshal`, `shelve`
- **network** — `socket`, `urllib`, `http`, `requests`, `httpx`

The federal seed (`_FEDERAL_SEED_IMPORTS`) is `{"__future__", "arcagent"}` —
just enough that the `@tool` decorator import and `from __future__ import
annotations` always validate, so a tool *can* be authored at federal at all.
Any unknown tier falls toward the stricter enterprise blocklist (fail-closed).

### What stays unconditionally blocked — every tier

`ImportPolicy` only relaxes **module imports**. The sandbox-escape checks in
`AstValidator` are enforced at *all* tiers, including personal, and cannot be
configured off:

- **Dynamic code execution** — calls to `eval`, `exec`, `compile`, `__import__`
  (`_BLOCKED_CALLS`).
- **Frame / interpreter traversal** — `f_back`, `f_globals`, `f_locals`,
  `f_builtins`, `gi_frame`, `tb_frame`, and the class-graph escape hatches
  `__class__`, `__bases__`, `__subclasses__`, `__mro__`, `__dict__`,
  `__reduce__`, `__init_subclass__`, `modules`, … (`_BLOCKED_ATTRIBUTES`).
- **Assignment to interpreter internals** — `__builtins__`, `__loader__`,
  `__spec__` (`_BLOCKED_ASSIGN_TARGETS`), and starred unpacking of them.
- **Non-UTF-8 source** — a PEP 263 coding declaration other than utf-8 is
  rejected *before* the AST is parsed (codec-stage attacks run before parsing).
- **`AttributeError.obj`/`.name` leaks**, `__init_subclass__` definitions, and
  metaclasses defining `__getitem__` — narrower CVE-class bypasses.

This is deliberately narrow-by-design: reject anything not understood. Even at
personal tier where all imports pass, an agent-authored tool still cannot
`eval`, reach a frame, or mutate builtins.

### What an agent may do re: creating tools, per tier

Authoring (`create_tool` / `update_tool`) validates against the **same**
tier-resolved `ImportPolicy` the loader will later enforce (`_runtime.import_policy()`),
so a tool accepted at authoring is never one the loader would reject for imports,
and vice-versa. But authoring is not authorization:

- **personal** — an agent can author, sign (automatically), and load its own
  tools with any imports, no operator step. This is what makes the default
  experience work: the scaffolded `calculator.py` (signed at `arc agent create`)
  and an agent's own self-authored tools load without the operator flipping any
  global switch.
- **enterprise / federal** — an agent can still *write* a tool, but it will not
  *load* until an operator approves it (see TOFU below). Federal additionally
  restricts which imports the tool may even contain.

---

## 2. Where capabilities load from — and their trust level

`CapabilityLoader.scan_and_register` walks four scan roots in precedence order
(a later root shadows an earlier one by name). Each capabilities root also
contributes a `*-skills` sub-root (its `skills/` subdir, where `create_skill`
writes):

| # | Root | Path | Trust |
|---|---|---|---|
| 1 | `builtins` / `builtins-skills` | `arcagent/builtins/capabilities/` | **trusted** |
| 2 | `global` / `global-skills` | `~/.arc/capabilities/` | **untrusted** |
| 3 | `agent` / `agent-skills` | `<agent_root>/capabilities/` | **untrusted** |
| 4 | `workspace` / `workspace-skills` | `<agent_root>/workspace/capabilities/` | **untrusted** |

Trusted vs untrusted is the set `_UNTRUSTED_ROOTS` in `capability_loader.py`.
Only the harness's own shipped package code (`builtins*`, and `module:*` module
capabilities) is trusted.

**What "untrusted" changes:** any root an agent can write to is untrusted —
including `global` and `agent`, because a compromised agent can plant a `.py`
there via bash and reload it, not just in its own workspace. Untrusted sources go
through the full gate: **AST validation → restricted builtins → signature verify
→ TOFU**. Trusted roots skip that gate and load directly (they are the harness
itself).

The load body for an untrusted `.py` (`_register_python_file`): AST-validate
(cached by `AstValidationCache` on md5+mtime), run the trust gate, then execute
the module with a hardened `__builtins__` from `build_restricted_builtins(policy)`
— no `open`/`eval`/`exec`, and an `__import__` that mirrors the AST import policy
at runtime. A skill folder (`_register_skill_folder`) is gated the same way,
keyed on its `SKILL.md`, because `SKILL.md` is injected into the agent prompt
(LLM01 / ASI06) and so is as trust-sensitive as executable code.

> Note: `build_restricted_builtins` is defense-in-depth / a fast-fail linter in
> front of the real OS execution sandbox (SPEC-036) — the object graph is
> escapable; genuine isolation is the sandbox's job, not this dict's.

---

## 3. Signing — what a signature proves, and what it does not

Signing primitives live in `arctrust.artifact`. An agent-authored artifact `X`
gets a **detached** signature written to an `X.arcsig` sidecar (the on-disk
convention is owned by `arcagent.capabilities.artifact_signing`):

```
workspace/capabilities/greet.py          # the artifact
workspace/capabilities/greet.py.arcsig   # ArtifactSignature JSON sidecar
```

The sidecar (`ArtifactSignature`, a frozen Pydantic model) carries everything a
verifier needs with no external lookup: the `sha256:` content digest, the signer
DID, the signer's Ed25519 public key, and the signature.

**Who signs.** `create_tool` / `update_tool` sign on write, using the agent's
**own DID key** via `_runtime.sign_artifact_file` → `artifact_signing.write_signature`
→ `arctrust.sign_artifact`. A plain `write` or `edit` does **not** sign (though
`resign_if_previously_signed` refreshes an *already-signed* file's sidecar so a
hand-edit of a signed artifact doesn't silently strip its signature).

**What a valid signature PROVES** (`verify_artifact`, re-run at load, fail-closed):

1. **Integrity** — the artifact bytes are unmodified since the signer wrote them
   (the content digest matches).
2. **Authorship / attribution** — the bytes are attributed to the signer's DID
   key; when a `trusted_public_key` is pinned, the signature must be that exact
   key.

**What it does NOT prove: authorization.** A compromised agent produces a
perfectly valid signature over malicious bytes with its own key. So a signature
is an *attribution boundary*, not a *permission*. Safety belongs to the TOFU gate
and the execution sandbox — never to the signature primitive.

The loader always pins `trusted_public_key = agent._identity.public_key`
regardless of tier (`agent_lifecycle.py`), so a self-signature is a real
attribution boundary: an attacker who can write into the workspace cannot forge
it without the agent's private key.

**Signed vs unsigned outcome, by tier:**

- A file written by `create_tool` is signed → at personal it loads; above
  personal it still needs TOFU approval.
- A file written by plain `write` is unsigned → above personal the
  `require_signature` floor denies it outright (`status: "unsigned"`) before TOFU
  is even consulted. At personal it needs the `auto_run_agent_code` toggle.

When signing fails or the agent has no signing identity, the authoring tool does
**not** pretend success — it appends a visible `WARNING: … is UNSIGNED and will
be denied at next load` (`_runtime.audit_unsigned_artifact`).

---

## 4. TOFU — Trust-On-First-Use source approval

`arctrust.tofu.TofuLayer` is the per-tier source-approval gate. It evaluates a
`CapabilitySource(name, source, signed)` and returns a `TofuDecision`:

| Decision | Meaning |
|---|---|
| `ALLOW` | source is approved (or the tier permits it) — load it |
| `DENY` | refuse — tamper/drift, or unsigned at a tier that requires signing |
| `NEW_SIGHTING` | first sight of this name — an operator must approve before it loads |

### Per-tier evaluation (`TofuLayer.evaluate`)

- **personal** — `signed → ALLOW`. Otherwise `ALLOW` only if the
  `auto_run_agent_code` toggle is on, else `DENY`. (An agent's own signed tools
  load; unsigned code needs the explicit opt-in.)
- **enterprise** — match by **name** against the approved pins:
  - unknown name → `NEW_SIGHTING`
  - known name + matching source hash → `ALLOW`
  - known name + **different** hash → `DENY` (drift = tamper = hard stop)
- **federal** — a valid signature is the **floor** (`unsigned → DENY`), then the
  *same* human-approval gate as enterprise applies. Federal = signed **AND**
  operator-approved: strictly stronger than enterprise. A self-signature
  attributes the code; it does not authorize it, so first-sight signed code
  still routes to `NEW_SIGHTING` → operator approval.

### The approval store — `[security.validators]`

Approvals persist in the agent's `arcagent.toml`, in the `[security.validators]`
block (`arctrust.validators`). This is the **sole persistence surface**, it lives
at agent root (never inside the workspace), and **the agent has no write access
— only a human operator mutates it.**

```toml
[security.validators]
auto_run_agent_code = false          # personal-only escape hatch; enterprise/federal keep false

[[security.validators.approved]]
name = "greet"                        # the loader's PIN KEY: a tool's file stem
hash = "sha256:9f2b…"                 # sha256 of the approved source bytes
approver = "did:arc:acme:operator/1a2b…"
timestamp = "2026-07-15T14:03:00Z"

[[security.validators.approved]]
name = "summarize-thread"             # for a skill, the FOLDER name (SKILL.md's parent)
hash = "sha256:04c7…"
approver = "did:arc:acme:operator/1a2b…"
timestamp = "2026-07-15T14:05:12Z"
```

The default is federal-safe: `auto_run_agent_code = false`, zero approved
entries. `hash_source(source)` produces the canonical `sha256:<hex>` that both
the loader (matching a pin) and the `arc trust` surfaces (recording one) agree
on. `approve_source` supersedes any prior pin for the same name, so re-approving
after drift replaces the stale hash. Writes are atomic (tempfile + `os.replace`)
and round-trip the rest of the file (comments, key order) via tomlkit.

### The operator flow

The agent discovers *which* capabilities are gated; the operator *approves*
them. This split is the layering (see §6): `arcagent.capabilities.inventory`
lists gated items, `arctrust.approve` / `arctrust.disapprove` mutate the store.

**CLI** (`arc trust`, in `arccli.commands.trust`):

```
arc trust list [--agent <id>] [--all]        # show gated (non-loaded) capabilities
arc trust approve <name> [--agent <id>]      # pin the current source hash
arc trust disapprove <name> [--agent <id>]   # remove a pin (drift / revoke)
```

`approve` reads the gated item's current source, calls `arctrust.approve` with
the loader's pin name (`pin_name_for` — a tool's file stem, a skill's folder
name), records the approver as the on-box operator DID (`~/.arc/operator`), then
re-scans and reports the post-approval verdict. It even warns you if you approve
at personal tier, where pins aren't consulted at load:

```
Approved greet on acme-analyst — status now: loaded (approver did:arc:acme:operator/1a2b…).
```

**arcui** (`arcui.routes.trust`) is the same operation over HTTP —
`GET /api/trust/gated`, `POST /api/trust/approve`, `POST /api/trust/disapprove`.
Mutations are gated on the `operator` role, record the same operator DID as
approver, and audit every mutation.

---

## 5. End-to-end pipeline

```
AUTHOR                     LOAD-GATE (per file, untrusted roots only)              REGISTER + INVOKE
──────                     ───────────────────────────────────────               ─────────────────
create_tool/update_tool
  │  AST-validate vs
  │  tier ImportPolicy
  │  (authoring mirror)
  ▼
 write <name>.py
  │
  ▼
 sign_artifact_file  ──►  <name>.py.arcsig   (agent DID key; plain write = no sidecar)


CapabilityLoader.scan_and_register
  │
  ├─ trusted root (builtins*)?  ──────────────────────────────────►  load directly
  │
  └─ untrusted root (workspace/global/agent[+-skills]):
        │
        1. AstValidationCache.validate          # imports (per tier) + always-on
        │      fail → status "invalid"          # eval/exec/frame escapes
        ▼
        2. _passes_trust_gate:
             a. require_signature (ent/fed)?     # floor
                  verify .arcsig vs pinned key
                  missing/invalid → status "unsigned"  ──► DENY
             b. TofuLayer.evaluate(name, source, signed):
                  ALLOW         ──────────────────────────►  proceed
                  NEW_SIGHTING  ──► status "new_sighting"    (operator must approve)
                  DENY          ──► status "deny"            (drift / unsigned-at-federal)
        ▼
        3. exec module under build_restricted_builtins(policy)
        ▼
   register into CapabilityRegistry  ──►  status "loaded"  ──►  tool invocable
                                                                (subject to arctrust.policy
                                                                 PolicyPipeline at CALL time)
```

**Where the tiers differ in this flow:**

| Stage | personal | enterprise | federal |
|---|---|---|---|
| AST imports | allow-all | blocklist (4 groups) | allowlist (deny-by-default) |
| AST escapes (eval/exec/frame) | blocked | blocked | blocked |
| signature floor | not required | **required** | **required** |
| TOFU | signed → ALLOW; else `auto_run_agent_code` | name+hash pin; new → `NEW_SIGHTING` | signed floor **then** name+hash pin |

Every gate is **fail-closed**: an AST error, an unreadable/forged sidecar, a
required signature with no pinned key, or any exception inside the trust gate all
DENY. Nothing unsigned or un-adjudicated registers.

Load approval (`TofuDecision`) is a distinct concern from **invocation** policy:
even after a capability loads, every tool *call* is separately evaluated by
`arctrust.policy.PolicyPipeline` (first-DENY-wins, fail-closed). TOFU gates
*load*; the policy pipeline gates *use*. They intentionally have separate
decision types (`TofuDecision` vs `Decision`).

---

## 6. Why the split: arcagent → arctrust

The trust boundary was recently corrected so that layering is clean:

- **`arctrust` owns trust.** Signing (`artifact`, `signer`), the TOFU gate
  (`tofu`), the approval store and its mutations (`validators.approve` /
  `disapprove`), operator-key custody (`operator`, `paths`), and the invocation
  policy pipeline (`policy`) all live here. `arctrust` is the leaf of the Arc
  dependency graph — it imports nothing from other Arc packages.
- **`arcagent` consults it at load.** The `CapabilityLoader` owns *discovery*
  (which artifacts exist across the scan roots, which did/didn't load) and calls
  into `arctrust` for every trust decision. It never re-implements a hash, a
  signature check, or an approval — it imports `CapabilitySource`, `TofuLayer`,
  `hash_source`, `verify_artifact` from `arctrust`.
- **`arccli` / `arcui` are thin operator surfaces.** They *list* gated items via
  `arcagent.capabilities.inventory` (discovery = arcagent) and *mutate* the store
  via `arctrust.approve` / `disapprove` (approval = arctrust). No trust logic of
  their own.

The rule of thumb: **discovery is arcagent, approval is arctrust.** A caller
lists what's gated (arcagent), then pins a hash (arctrust). This keeps the
security-critical trust logic in one auditable, sibling-free leaf package.
