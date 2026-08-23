## Build Principles

### 1. Simplicity

- Prefer flat, explicit code over clever abstractions.
- Core stays under **5,000 LOC** (ADR-004).
- Complexity lives in extensions, plugins, and modules — never in the nucleus.
- If you need a comment to explain control flow, refactor.
- Nesting deeper than 2 levels → extract a named method.
- One class, one responsibility. One method, one job.

### 2. Security

Federal-first. This runs on DOE machines, in labs, in SCIFs.

#### Four Pillars are universal — not federal-only (ADR-019)

Every deployment, at every tier (personal / enterprise / federal), enforces:

1. **Identity** — every entity has a DID. `ArcAgent.__init__` requires it. Every tool dispatch carries `caller_did`. Primitives live in `arctrust`.
2. **Sign** — every loaded artifact (skill, extension, backend, pairing) is verified before use. No `UnsafeNoOp`, no `skip_sandbox`, no `require_manifest = federal-only`. `arctrust.keypair` + Sigstore + Rekor.
3. **Authorize** — `arctrust.policy.PolicyPipeline` on every tool call. First-DENY-wins. Fail-closed on exceptions.
4. **Audit** — `arctrust.audit.emit(AuditEvent, sink)` on every operation. Single emission point; sinks fan out: `JsonlSink` (compliance), `SignedChainSink` (tamper-evident chain), `arcui.bridge.UIBridgeSink` (live observability).

**Tier is stringency metadata, not a gate.** Federal: FIPS-validated crypto, signed allowlists, hard `max_turns` cap, all 5 policy layers. Personal: self-signed bundles (audit warn), Global-only policy layer, dynamic tool creation. **Every tier still identifies, verifies, authorizes, and audits.**

#### Assume an active attacker is already inside

Zero trust is the default threat model, not a network topology. Design every boundary as though an attacker can read and modify ordinary files, call public APIs, replay messages, invoke package entry points, and inspect local traffic.

The security boundary is cryptographic identity plus explicit authorization:

- **Tools, skills, modules, extensions and backends:** direct filesystem edits never change what an agent trusts or can load. Every executable artifact is content-addressed, signed, verified at load and again when it changes, bound to an approved manifest and agent scope, then authorized before use.
- **Prompts and instructions:** system prompts, identity, policy, workflow definitions and other control-plane instructions are protected artifacts, not ordinary mutable text.
- **Keys and credentials:** operator and agent private keys are non-exportable. Code receives a signing/decryption capability or short-lived handle, never raw key material.
- **Runs and manual invocation:** starting, resuming, steering or replaying an Arc agent requires a fresh, signed and authorized request with nonce/timestamp replay protection and an audit event.
- **Logs, traces and transcripts:** LLM inputs/outputs, run events, tool I/O, traces and audit records are sensitive data. Encrypt them at rest and in transit.
- **Data stores and messages:** authenticate and authorize every query, mutation and subscription.
- **Failure behavior:** verification, key custody, policy, audit, encryption or identity failures fail closed.

#### Other invariants

- Secure by default, not by configuration.
- Zero-trust everything: identity, comms, data, modules.
- Full observability: OpenTelemetry traces, metrics, structured logs on every action.
- Tamper-evident logging with classification awareness.
- Credentials never touch the filesystem — vault-backed, short-lived tokens only.
- **Lethal Trifecta:** private data + external comms + untrusted input never coexist without human approval.
- mTLS on all internal communications.

### 3. Scalability

Built for thousands of agents running concurrently.

- Shared-nothing per agent; coordinate via message bus (NATS).
- Async-first (`asyncio` + `uvloop`).
- Fail gracefully: circuit breakers, exponential backoff, failover chains.
- Cold start < 500ms; baseline memory < 50MB per agent.
- Horizontal scale; no singleton bottlenecks.
- Connection pooling, resource limits, and timeouts on everything external.

### 4. Composability

> **Standalone primitives. Turnkey whole. Federal-securable by construction.**

No line of code may sacrifice one of these audiences for another:

1. **Developer who wants one layer** — `pip install arcllm` (or `arcrun`) alone, with its own contract, without pulling higher layers.
2. **Non-technical user who wants everything** — layers compose into a turnkey stack that works with **zero configuration**.
3. **Federal operator who hardens later** — personal → federal is a *stringency dial*, not a rewrite.
4. **Maintainable** - we can isolate and completely rewrite a module and as long as input and output format is the same, it will still work.

**How we keep all four open**

| Rule | Meaning |
|------|---------|
| **Dependencies point one way, never up** | `arcrun` → `arcllm`; `arcagent` → `arcrun` **only**; `arctrust` is a leaf. |
| **One root import, qualified names** | Write `import arcllm`, `import arcrun`, or `import arcagent` only. |
| **One contract per seam; default unbreakable** | Typed seam; base impl correct with zero config. |
| **Security seams from day one** | Identity, Sign, Authorize, Audit at every seam at every tier. |
| **Concern purity enables all four** | See Non-Negotiable §1. |

---

## Components

### Module vs Extension

For all packages:

- **Module** — An Arc plugin that adds a feature or capability that comes with arc and can be installed directly.
- **Extension** — Arc feature or capability that is created by external parties and can interact with Arc. It may include scripts to install, add files, etc.

All of these are:

- Completely removeable from the directory with no loss of function to the package (except that specific capability). So fully optional.
- The importing package has no need or knowledge off it other than the import.
- Optional installs at the cli level
- Optional imports, imported separately and specifically

### The seam is the product boundary

Everything outside a package nucleus is a replaceable layer, module, adapter or plugin behind one explicit typed contract. The core knows the contract and a default implementation; it never knows a vendor, feature directory or concrete class. Every implementation accepts the same canonical input model and returns the same canonical output/event model so it can be removed, replaced or rewritten without changing callers.

- Adding an implementation means registering/discovering a leaf component, not adding a core `if provider == ...` branch.
- Removing its files must leave imports, startup and every unrelated feature working. Only that capability may become unavailable, through a typed degraded result rather than an import error or partial initialization.
- Optional dependencies are imported inside the plugin boundary only. They never leak into public contract types or become transitive requirements of the core.
- Plugins own their state and lifecycle. Registration returns an ownership token; teardown removes every handler, tool, task, resource and cache entry it added.
- Cross-layer calls use the public root facade and the adjacent seam only. No deep imports, reverse imports, shared mutable globals, database-table reach-through or side-channel callbacks.
- Contract tests run against the default, fake and every real implementation. Architecture tests prove the package starts with each optional component physically absent.

If a feature cannot be deleted from the tree without breaking unrelated behavior, or cannot be replaced while preserving its input/output contract, it is not properly seamed and must not merge. See `docs/concepts/seam-model.md`.

### A data connection is not complete when its tools work

Every connection to a data-bearing system must implement both independent seams:

1. Interactive tools for direct agent actions; and
2. The canonical connected-source lifecycle for Knowledge discovery, resource selection, mapping approval, incremental sync/reindex/revoke, provenance and agent retrieval.

The connection card must show Knowledge status for every granted agent and offer the complete configure-and-sync journey. Shipping tool calls without enrollment, or an adapter without a usable ArcUI/ArcCLI path, is incomplete. Contract tests must start from a real granted connection and prove it becomes searchable agent knowledge. A genuinely non-indexable security system must declare that explicitly with a threat-model reason; it must never silently disappear from Knowledge.