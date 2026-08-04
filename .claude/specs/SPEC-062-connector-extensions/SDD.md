# Solution Design Document: SPEC-062 Connector Extensions

## Context References

- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Project structure:** [.claude/steering/structure.md](../../steering/structure.md)
- **PRD:** [PRD.md](./PRD.md)

## Overview

One mechanism for attaching any external system to an agent. An extension is a signed bundle living outside Arc; Arc exposes a single hook contract it plugs into. The hook — `ExtensionAttachment` — is the whole design: it is what a protocol-server extension, a direct-implementation extension, and a skills-only extension all satisfy, and it is why adding the eleventh connection touches no core file. Everything else in this SDD either produces an attachment (loader, manifest, transports) or governs one (registry bridge, launcher, ledger, credential seam). MCP is finished as one implementation of the hook, not as the hook itself.

## Architecture

Five bands, dependencies pointing down only, per `.claude/steering/structure.md#layer-model`.

**1. Manifest and trust.** `ExtensionManifest` (Pydantic, `extra="forbid"`) is the only thing Arc parses from an extension. `ExtensionLoader` resolves a bundle through a NEW UNTRUSTED capability root so the existing trust gate in `capabilities/capability_loader.py:323-376` evaluates it — bundles never ride a module scan root, which is trusted (`capability_loader.py:84-94`, roots appended at `agent_lifecycle.py:152-154`). Signature verification reuses the `.arcsig` sidecar convention in `capabilities/artifact_signing.py` unchanged.

**2. The hook.** `ExtensionAttachment` is a Protocol with four methods — `requirements()`, `probe()`, `describe_tools()`, `invoke()`. Two implementations ship: `McpAttachment` (speaks MCP) and `NativeAttachment` (imports a callable the extension package exposes). A third party writes either shape, or ships only skills and implements none. Core knows the Protocol; it never knows a vendor.

**3. Execution.** `ProcessLauncher` is the single spawn path for every extension process at every tier; a `SandboxPolicy` value is the only thing that varies, defaulting to no confinement at personal. `ArtifactPinVerifier` checks version and hash before each execution. The env safety filter strips interpreter-startup and loader variables.

**4. Governance.** `CapabilityBridge` converts each allowlisted tool into a `RegisteredTool` (`tools/_transport.py:36-74`) carrying `transport`, `classification`, and `capability_tags`, then registers it — which is all that is needed to ride the existing envelope in `core/tool_registry.py:384-600`: schema validation, signed `ToolCall`, `PolicyPipeline` under the admission lock, `HumanGate` for forbidden compositions, timeout, audit. `ToolContractLedger` defends the rug-pull the envelope cannot see. `AuditRedactor` runs in the module before `arctrust.audit.emit`, because `arctrust` is a leaf and must not import `arcllm`.

**5. Surfaces and lifecycle.** The `mcp` module (`arcagent/modules/mcp/`, shipping `capabilities.py` + `_runtime.py` per `core/module_discovery.py:40-52`) hosts the runtime, disabled by default. CLI verbs are the complete surface; arcui calls the same functions.

## Components

### COMP-001: ExtensionManifest
**Responsibility:** Pydantic model for `extension.toml` — the single document Arc parses from an extension. Declares: attachment kind, host prerequisites, pinned artifact (package, version, sha256), tool allowlist, per-tool classification and capability tags, required secrets, declared dependencies, tier floor, and default approval mode. Sets `extra="forbid"` so a typo raises rather than vanishing (following `core/module_config.py:14-21`). Carries a denied-key list modelled on `_DENIED_OVERLAY_PATHS` (`arccli/blueprints.py:73-83`) so a lower-trust manifest cannot reach vault backends, process tools, or key directories. Refuses an unbounded tool allowlist above personal tier.
**Dependencies:** _(none)_
**Inputs:** extension.toml text
**Outputs:** validated ExtensionManifest, or ValidationError naming the offending key

### COMP-002: ExtensionCatalog
**Responsibility:** Resolves an extension name to a bundle and records whether it is official. Validates names against a strict pattern to block traversal, exactly as `arcgateway/adapters/registry.py:79-90` does for adapters. Holds the vetted-upstream allowlist mapping name to expected distribution plus pinned version and hash — the same control point as `OFFICIAL_ADAPTERS` (`registry.py:61-65`): an unlisted bundle warns with an audit event below federal and is refused at federal.
**Dependencies:** COMP-001
**Inputs:** extension name, deployment tier
**Outputs:** bundle path + official/unofficial verdict, or refusal with audit event

### COMP-003: ExtensionLoader
**Responsibility:** Loads a bundle through a NEW untrusted capability root so the existing trust gate runs on every load, independently of any install-time check. Verifies the `.arcsig` sidecar via `capabilities/artifact_signing.verify_file`, enforces that requiring a signature implies a pinned key (`capability_loader.py:334-337`), and denies on any exception. Routes the bundle's parts by kind: skills and tools to the agent capability folder, implementation and dependencies left in the extension folder, host prerequisites handed to COMP-018.
**Dependencies:** COMP-001, COMP-002, COMP-018, COMP-004
**Inputs:** bundle path, agent identity, tier, pinned operator key
**Outputs:** an ExtensionAttachment plus registered capability paths, or a fail-closed denial with audit event

### COMP-004: ExtensionAttachment (the hook contract)
**Responsibility:** THE seam this spec exists to build. A Protocol with four methods: `requirements()` returns what the host and the operator must supply; `probe()` proves the connection works and returns the live tool list; `describe_tools()` returns tool specs (name, description, input schema, classification, capability tags); `invoke(tool, args)` executes one call and returns a result. Core depends only on this Protocol. Adding a new connection means providing an implementation, never editing core — this is what makes REQ-278 and REQ-280 testable.
**Dependencies:** _(none)_
**Inputs:** Protocol implemented by an extension; invoked with (tool_name, validated args)
**Outputs:** requirement list, probe result, list[ToolSpec], and per-call results or structured errors

### COMP-005: McpAttachment
**Responsibility:** Implements COMP-004 by speaking MCP over httpx and stdio, with no vendor SDK (CON-7). Targets the 2026-07-28 stateless revision: no initialize handshake, no session id, `_meta` protocol version on every request, `server/discover` used as the legacy probe. Handles the two-tier error split — JSON-RPC errors are protocol failures, `isError: true` results are tool failures the agent can reason about — and honours `resultType` so an `input_required` round trip is not mistaken for completion. Applies timeout, bounded retry with backoff, and a circuit breaker.
**Dependencies:** COMP-004, COMP-008, COMP-010
**Inputs:** server definition (command/args or url/transport), resolved credential handle, tool name and args
**Outputs:** tool specs from tools/list; call results from tools/call; structured errors on failure

### COMP-006: NativeAttachment
**Responsibility:** Implements COMP-004 for extensions that ship their own implementation instead of a protocol server — the 1Password SDK client and the Jira/Confluence REST client are the first two. Imports a well-known factory from the extension package (the generic `provider_entrypoint` convention already used by `arcagent/extension/point.py`) and adapts its callables to the hook. The extension owns all service knowledge; this component owns none.
**Dependencies:** COMP-004, COMP-010, COMP-017
**Inputs:** dotted entrypoint from the manifest, resolved credential handle
**Outputs:** list[ToolSpec] and per-call results, identical in shape to COMP-005

### COMP-007: ToolContractLedger
**Responsibility:** Defends the rug-pull the dispatch envelope cannot see. Records a hash over each approved tool's name, description, and input schema at approval time; recomputes on every tool-list retrieval. A changed hash suspends that tool, emits an audit event, and requires explicit re-approval. This is the only defence that works for hosted attachments where there is no artifact to pin. Upstream tool `annotations` are ignored in favour of the manifest, per MCP spec guidance on untrusted servers.
**Dependencies:** COMP-019
**Inputs:** list[ToolSpec] from an attachment, previously approved hashes
**Outputs:** per-tool verdict (unchanged | changed-and-suspended | newly-seen), audit events

### COMP-008: ProcessLauncher and SandboxPolicy
**Responsibility:** The single path by which any extension process is started, at every tier. A `SandboxPolicy` value is the only thing that varies — none at personal by default, container-level at enterprise, microVM at federal — so the federal path is the same path, satisfying the stringency-dial rule in `.claude/steering/tech.md`. Strips interpreter-startup and dynamic-loader environment variables before execution. Starts a process lazily on first use and reaps it after an idle threshold.
**Dependencies:** COMP-009
**Inputs:** process definition, SandboxPolicy, scrubbed environment
**Outputs:** a running process handle, or a refusal with audit event; idle processes reaped

### COMP-009: ArtifactPinVerifier
**Responsibility:** Verifies that the third-party artifact about to run is exactly the one that was approved: exact version plus integrity hash from the manifest, checked before each execution rather than only at install. A mismatch refuses the run and emits an audit event. Forbids fetch-at-launch, which is what makes a bare `npx -y <package>` style invocation unrepresentable in a manifest.
**Dependencies:** COMP-001
**Inputs:** declared package, version, sha256; resolved artifact on disk
**Outputs:** pass, or refusal with audit event naming expected and actual hash

### COMP-010: SecretStore seam
**Responsibility:** One interface for resolving and persisting connector credentials; the backing store is selected by deployment tier. Default is the per-agent local store at owner-only permissions; enterprise and federal select an external vault without any change to calling code. Never writes a secret into a config file, a log, a prompt, or model context. Secrets are handed to attachments as late as possible and by handle where the attachment supports it.
**Dependencies:** _(none)_
**Inputs:** (agent, instance, field) and a secret value on write; the same key on read
**Outputs:** resolved secret or handle; write confirmation; never the value in any log

### COMP-011: CredentialLifecycle
**Responsibility:** Keeps connections alive unattended. Renews proactively at a fraction of credential lifetime rather than reactively on failure. Enforces at most one renewal in flight per connected account and persists atomically, so a rotating single-use refresh token cannot be destroyed by concurrent renewal or a torn write. Distinguishes transient failure (retry with backoff) from terminal failure needing human re-consent, and escalates the latter through the operator approval path rather than agent chat.
**Dependencies:** COMP-010, COMP-019, COMP-013
**Inputs:** stored credential record (expiry, issuer, audience, last refresh)
**Outputs:** renewed credential persisted atomically, or a connection marked needing-attention plus an operator escalation

### COMP-012: CapabilityBridge
**Responsibility:** Turns tool specs from any attachment into `RegisteredTool` instances (`tools/_transport.py:36-74`) and registers them, which is the entire cost of riding the existing envelope in `core/tool_registry.py:384-600`. Each tool is registered under its own name so policy, audit, and approval see the real verb rather than a generic dispatcher. Supplies `classification` (defaulting to the restrictive value) and `capability_tags`, which are what feed `legs_for_call` and therefore the trifecta gate. Composes the manifest's per-server allowlist with the existing `ToolConfig.allow`/`deny` filter (`core/config.py:132-133`) rather than duplicating it, deny-wins.
**Dependencies:** COMP-004, COMP-007, COMP-001
**Inputs:** list[ToolSpec], manifest allowlist, agent tool policy
**Outputs:** registered named capabilities; skipped tools recorded as policy-denied without raising

### COMP-013: ApprovalBinding
**Responsibility:** Translates the per-instance approval mode from configuration into the existing gate. Default admits inbound reads and requires approval for every outbound call; an operator may relax it per connected account. Routes a gated call through the mechanical approval path so the grant is signed and pinned to the operator identity rather than accepted conversationally, and presents the request naming the instance and the outbound target so the decision is legible.
**Dependencies:** COMP-012
**Inputs:** instance approval mode, tool classification and capability tags, call arguments
**Outputs:** proceed, or suspend-and-request-approval; resumption on a signed grant

### COMP-014: AuditRedactor
**Responsibility:** Records every connector call and response in full — inputs and outputs — with classification labels, satisfying reconstruction requirements. Applies exactly one carve-out: a credential read records store, item, field, caller, and outcome but never the value. Reuses the existing detector in `arcllm/_pii.py` (`SECRETS_CATEGORY`, `PiiDetector`, `redact_text`) and runs INSIDE this module before handing the event to `arctrust.audit.emit`, because `arctrust` is a leaf and importing `arcllm` from it would invert the dependency DAG.
**Dependencies:** _(none)_
**Inputs:** call arguments, call result, tool classification
**Outputs:** an AuditEvent with full content or the credential carve-out applied, emitted through the single emission point

### COMP-015: Connector module (arcagent/modules/mcp)
**Responsibility:** Hosts the runtime as an optional module shipping both `capabilities.py` and `_runtime.py`, which is what `core/module_discovery.py:40-52` requires for discovery. Disabled by default so an existing agent gains no behaviour until its configuration enables it. Requests only the narrowest kwargs from the signature-dispatched `configure` in `core/agent_lifecycle.py:216-269`, since core offers signing authority only to modules that name it. Holds runtime state on a `contextvars.ContextVar`, never a module global, per the precedent documented in `modules/scheduler/_runtime.py:1-19`.
**Dependencies:** COMP-003, COMP-012, COMP-008, COMP-011
**Inputs:** module config, agent identity, tier, policy pipeline, human gate
**Outputs:** attachments loaded and tools registered on enable; clean teardown on disable

### COMP-016: Connector CLI
**Responsibility:** The complete management surface: add, authorize, list, inspect, manage allowlist, probe, doctor, approve a changed tool contract, and remove. `add` reads the manifest, prompts for each declared secret with non-echoing input, probes, and persists only on success — nothing partial is ever written, extending the validate-before-write discipline already in `arcgateway/connect.py:27-56`. Prompting lives here; the write path lives with the module so other surfaces reuse it, mirroring how `connect.py` is placed for arcui's benefit.
**Dependencies:** COMP-003, COMP-010, COMP-017, COMP-018, COMP-022
**Inputs:** operator commands and interactive responses
**Outputs:** installed and probed connection, or a failure naming the step; no partial state

### COMP-017: DependencyResolver
**Responsibility:** Handles declared dependencies. At install, checks the declared requirements against what other extensions already declare and refuses on an unsatisfiable version conflict rather than silently breaking a working extension. At removal, reference-counts declarations and drops what nothing else needs. Keeps a record of which extension declared what, so removal is decidable rather than guessed.
**Dependencies:** COMP-001, COMP-019
**Inputs:** manifest requirement list, current declarations across installed extensions
**Outputs:** install plan or conflict refusal; removal plan naming exactly what is safe to drop

### COMP-018: HostPrerequisiteDirector
**Responsibility:** Handles what the machine must provide — a runtime, a binary, a system package. Detects whether the prerequisite is present and, when absent, tells the operator exactly what to install on the host. Never installs implicitly, because a machine-level change is the host's decision and must be visible; this is what keeps category-one placement honest.
**Dependencies:** COMP-001
**Inputs:** declared host prerequisites, host inspection
**Outputs:** satisfied, or an explicit instruction naming what the operator must install; never a silent install

### COMP-019: ConnectionStateStore
**Responsibility:** Records per-connection operational state in the operational data plane rather than in config: health, last successful use, credential expiry, approved tool-contract hashes, and dependency declarations. Lets management surfaces report status without probing the external service.
**Dependencies:** _(none)_
**Inputs:** state updates from attachments, ledger, lifecycle, and resolver
**Outputs:** queryable connection records for CLI, TUI, and web surfaces

### COMP-020: Management surfaces (TUI and web)
**Responsibility:** Presents connection status, approvals, and management actions by calling the same functions the CLI calls. The web surface is a convenience layer and is required for nothing; every action it offers exists at the command line. Secret entry, where offered, posts directly to the secret store and never enters model context. Surfaces read status from COMP-019 rather than probing.
**Dependencies:** COMP-016, COMP-019, COMP-010
**Inputs:** operator interaction
**Outputs:** the same effects as the equivalent CLI verb, through the same code path

### COMP-021: Mechanism conformance tests
**Responsibility:** Proves the mechanism is general rather than tailored. An architecture test fails if a vendor or service name appears in any core package outside test fixtures. A reference extension implementing only COMP-004 is added end to end with no core file modified — the governing test. Removability tests assert an agent starts and passes its smoke test with all extensions removed, and again with all optional modules removed. A fake attachment drives the real registry, policy, and audit path so dead wiring cannot pass green.
**Dependencies:** COMP-004, COMP-012, COMP-015
**Inputs:** the built system plus a reference extension fixture
**Outputs:** pass, or a named violation identifying the coupling

### COMP-022: Shared TOML writer
**Responsibility:** One TOML emitter, extracted rather than copied a third time. `_dump_toml`/`_emit_table`/`_scalar` already exist near-verbatim in both `arcgateway/connect.py:97-130` and `arccli/blueprints.py:461-483`; this feature needs the same thing, which is the third instance and the point at which the project's own three-instances rule fires. Both existing copies are deleted in the same change, per the no-legacy rule.
**Dependencies:** _(none)_
**Inputs:** a nested mapping of TOML-representable values
**Outputs:** deterministic TOML text; both prior copies removed


## Data Model

**extension.toml** (COMP-001), the only document Arc parses from an extension: an `[extension]` header (name, version, tier floor, attachment kind); an optional `[artifact]` pin (package, version, sha256) required whenever a third-party artifact is executed; `[[host_requires]]` entries for machine-level prerequisites; `[[secrets]]` declaring what the operator must supply and under what key; `requires` listing declared dependencies; `[tools]` carrying the allowlist plus per-tool classification and capability tags; and `[approval]` carrying the default mode. Unknown keys are rejected; denied keys are stripped with an audit note before the document is trusted.

**Signature sidecar**: `<bundle>.arcsig` alongside the bundle, holding an arctrust artifact signature — the existing convention in `capabilities/artifact_signing.py`, reused unchanged. A corrupt sidecar reads as unsigned.

**Operational records** (COMP-019), in the operational data plane rather than config: a connection record per (agent, instance) with health, last successful use, credential expiry, issuer and audience; approved tool-contract hashes per (instance, tool); dependency declarations per extension for reference-counted removal.

**Secrets** (COMP-010) are keyed by (agent, instance, field) and never appear in any config file, log, prompt, or audit record — the sole audit exception being the credential-read carve-out, which records the coordinates and not the value.

**Agent config** gains `[extensions.<instance>]` blocks, so one bundle can back several distinctly-named instances bound to different accounts — the same mechanism by which one gateway plugin backs several platform blocks (`arcgateway/adapters/registry.py:223-232`).

## External Integrations

Every external system is reached through COMP-004 and nothing else; core names none of them. The first implementations are chosen to exercise different shapes rather than to be exhaustive: a locally spawned protocol server with browser-bootstrapped OAuth and headless refresh; a locally spawned protocol server with device-code bootstrap; a hosted protocol endpoint with nothing to pin, which is precisely why COMP-007 exists; and two direct-implementation attachments, one wrapping a vendor SDK whose protocol server requires a desktop session and one wrapping a public REST API where no upstream met the vetting bar. Failure handling is uniform across all of them (COMP-005, COMP-006): timeout, bounded retry with backoff, circuit breaker, and tools that stay registered returning structured errors rather than disappearing from the agent's toolset. Rate limits and idempotency are the attachment's concern, declared in the manifest and enforced by the launcher's timeout and breaker.

## Traceability

| Requirement | Components |
|---|---|
| REQ-260 | COMP-016, COMP-003, COMP-001, COMP-010, COMP-022 |
| REQ-261 | COMP-016, COMP-003 |
| REQ-262 | COMP-018, COMP-001 |
| REQ-263 | COMP-003, COMP-012 |
| REQ-264 | COMP-003, COMP-001, COMP-017 |
| REQ-265 | COMP-010, COMP-016 |
| REQ-266 | COMP-012, COMP-004 |
| REQ-267 | COMP-012 |
| REQ-268 | COMP-001, COMP-002, COMP-012 |
| REQ-269 | COMP-001, COMP-012 |
| REQ-270 | COMP-012, COMP-005, COMP-006 |
| REQ-271 | COMP-005, COMP-006 |
| REQ-272 | COMP-008 |
| REQ-273 | COMP-008 |
| REQ-274 | COMP-013, COMP-001 |
| REQ-275 | COMP-013 |
| REQ-276 | COMP-014 |
| REQ-277 | COMP-014 |
| REQ-278 | COMP-004 |
| REQ-279 | COMP-001, COMP-004, COMP-005, COMP-006 |
| REQ-280 | COMP-021, COMP-004 |
| REQ-281 | COMP-003 |
| REQ-282 | COMP-003 |
| REQ-283 | COMP-003 |
| REQ-284 | COMP-015, COMP-003, COMP-017, COMP-021 |
| REQ-285 | COMP-015, COMP-021 |
| REQ-286 | COMP-015 |
| REQ-287 | COMP-011 |
| REQ-288 | COMP-011 |
| REQ-289 | COMP-011, COMP-013 |
| REQ-290 | COMP-009, COMP-001 |
| REQ-291 | COMP-007 |
| REQ-292 | COMP-008 |
| REQ-293 | COMP-016, COMP-020 |
| REQ-294 | COMP-010 |
| REQ-295 | COMP-019 |

## Alternatives Considered

**A generic dispatch tool instead of named registration.** One `call(server, tool, args)` tool would need no discovery and keep the prompt small. Rejected: policy and audit would only ever see the dispatcher, so no per-verb denial is expressible and every approval prompt is blind to what is actually happening. Named registration is the reason the envelope is worth anything.

**A new leaf package for the protocol client.** Cleaner standalone story and a smaller nucleus. Rejected because the operator requirement was that the capability be removable without the agent breaking, which a module gives and a dependency does not; complexity in modules rather than the nucleus also matches the project's own simplicity rule.

**Vendoring per-extension dependency environments.** True isolation, conflict-proof, and removal is a directory delete. Rejected by the operator in favour of declared dependencies installed into the shared environment — lighter and more familiar, at the cost that a version conflict between two extensions is unresolvable, which is why COMP-017 refuses the conflict at install rather than discovering it at runtime.

**Sandboxing only at enterprise and above.** Simplest laptop story. Rejected: it forks the execution path by tier, so the federal path would never be exercised during development, contradicting the stringency-dial rule. A single launcher with a no-op default policy gets the same simplicity without the fork.

**Loading bundles from a module scan root.** Simplest wiring and it reuses an existing root. Rejected: module roots are trusted, so third-party code would inherit trust it must not have — the exact inversion this design exists to prevent.

**Adopting the best-maintained upstream for every service.** Fastest breadth. Rejected for two services where the honest verdicts were a desktop-session requirement that cannot work headless, and a project whose mechanics fit but whose recent advisory history includes a critical remote-code-execution and a critical authentication bypass. Direct-implementation attachments cover both.

**A credential broker so the connector never receives the credential.** Strongest available answer to the supply-chain risk, and the pattern Anthropic's own sandbox runtime uses. Deferred to a follow-on rather than rejected: COMP-010 hands out handles where an attachment supports it, which is the seam a broker would slot into without redesign.

## Risks and Mitigations

**Tool poisoning is the top risk and the least intuitive.** An upstream can serve a clean tool list at approval and change it later; package pinning cannot see this, and hosted attachments have no package at all. Mitigated by COMP-007, whose test must assert suspension rather than merely detection.

**Trusted-root inversion.** Loading bundles anywhere trusted silently defeats the entire signature design. Mitigated by COMP-003 plus an explicit test that a bundle placed in a module root is refused.

**Rotating single-use refresh tokens destroyed by concurrency.** Two renewals in flight invalidate each other and a late writer persists a rejected token, breaking the connection until a human re-consents. Mitigated by COMP-011; the test must force interleaving with a barrier, since an instant mock would pass while the bug survives.

**Dependency conflict between two extensions** is unresolvable in a shared environment. Mitigated by COMP-017 refusing at install; the residual risk is that a later upgrade of one extension breaks another.

**Full audit capture makes the audit store the highest-value target on the host** and requires access control of its own. Mitigated partially by COMP-014's credential carve-out; the store's own access policy is out of scope here and should be raised.

**Dead wiring passing green** is this project's known failure mode: correct-looking predicates with nothing activating them. Mitigated by COMP-021 driving the real registry, policy, and audit path with a fake attachment rather than patching internals.

**Scope.** Nine subsystems in one spec. Mitigated by phasing the PLAN so the hook, one attachment, and the governed path are provable before breadth is added; the governing test is deliberately a Phase-1 deliverable rather than a final one.

**Constraints.** Core stays under its LOC budget (`.claude/steering/tech.md`); dependencies point one way and `arctrust` is a leaf, which is why redaction runs before emission rather than inside the sink.

## Open Questions

- Does the credential broker land in this spec or a follow-on? COMP-010 is designed as the seam it would slot into, but the decision affects how aggressively attachments are given handles rather than values.
- What is the retention and access policy for an audit store that now holds full message bodies? Full capture is required for reconstruction, but the store's own protection is not designed here.
- Which attachment ships first as the proof? The protocol-server shape with a well-maintained upstream and two existing operator accounts exercises the hook, named instances, and headless refresh in one integration.
- Should the tier floor in the manifest be able to raise a deployment's effective stringency for that connection, or only refuse to load below it?
