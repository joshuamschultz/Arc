# Implementation Plan: SPEC-062 Connector Extensions

## Context References

- **PRD:** [PRD.md](./PRD.md)
- **SDD:** [SDD.md](./SDD.md)
- **Tech stack:** [.claude/steering/tech.md](../../steering/tech.md)
- **Roadmap:** [.claude/steering/roadmap.md](../../steering/roadmap.md)

## Phase 1: Foundation

- [x] **T-877**: (red) Failing tests for ExtensionManifest: unknown key raises, denied keys stripped, wildcard allowlist refused above personal
  - domain: test
  - Components: COMP-001
  - Requirements: REQ-262, REQ-264, REQ-268, REQ-269, REQ-274, REQ-290
  - Acceptance: Tests assert extra=forbid raises on a typo, denied paths are dropped with an audit note, and an unbounded allowlist is refused above personal tier. All fail.
- [x] **T-878**: (green) ExtensionManifest Pydantic model and denied-key stripping
  - domain: backend
  - Components: COMP-001
  - Requirements: REQ-262, REQ-264, REQ-268, REQ-269, REQ-274, REQ-290
  - Acceptance: T-877 passes. Manifest parses artifact pin, host prerequisites, secrets, requires, tools with classification and tags, tier floor, approval mode.
- [x] **T-879**: (green) Define the ExtensionAttachment Protocol with no implementation
  - domain: backend
  - Components: COMP-004
  - Requirements: REQ-278, REQ-279, REQ-280
  - Acceptance: Protocol declares requirements, probe, describe_tools, invoke. mypy --strict passes. No core module imports any concrete attachment.
- [x] **T-880**: (red) Failing tests for ExtensionCatalog: name traversal refused, unofficial warns below federal and is refused at federal
  - domain: test
  - Components: COMP-002
  - Requirements: REQ-268
  - Acceptance: Tests cover ../evil and dotted names, and both tier behaviours for an unlisted bundle. All fail.
- [x] **T-881**: (green) ExtensionCatalog with name validation and vetted-upstream allowlist
  - domain: backend
  - Components: COMP-002
  - Requirements: REQ-268
  - Acceptance: T-880 passes. Allowlist maps name to expected distribution, version and hash; every verdict emits an audit event.
- [x] **T-882**: (red) Failing tests for the untrusted extension root: a bundle in a module root is refused; unsigned denied above personal; unpinned key never satisfies a signature requirement
  - domain: test
  - Components: COMP-003
  - Requirements: REQ-281, REQ-282, REQ-283
  - Acceptance: Tests drive the real capability loader, not a patched one. All fail.
- [x] **T-883**: (green) Add the extension untrusted root and wire the trust gate
  - domain: backend
  - Components: COMP-003
  - Requirements: REQ-281, REQ-282, REQ-283
  - Acceptance: T-882 passes. Bundles load only through the untrusted root; .arcsig verified at load independently of install; any exception denies.
- [x] **T-884**: (green) ExtensionLoader routes bundle parts by kind
  - domain: backend
  - Components: COMP-003
  - Requirements: REQ-263, REQ-264, REQ-260, REQ-261
  - Acceptance: Skills and tools land in the agent capability folder; implementation and dependencies stay in the extension folder; host prerequisites are handed off, never installed here.
- [x] **T-885**: (refactor) Extract the shared TOML writer and delete both existing copies
  - domain: backend
  - Components: COMP-022
  - Requirements: REQ-260
  - Acceptance: One emitter remains. arcgateway/connect.py and arccli/blueprints.py both import it; their local copies are gone in the same change. Existing tests for both callers still pass.
- [x] **T-886**: (green) ConnectionStateStore records and schema
  - domain: db
  - Components: COMP-019
  - Requirements: REQ-295, REQ-291
  - Acceptance: Per-(agent, instance) health, last successful use, credential expiry, approved tool-contract hashes, and dependency declarations persist and are queryable.

## Phase 2: Core

- [ ] **T-887**: (red) Failing tests for the CLI attachment: argv built from declared args only, JSON parsed, exit code mapped, no shell
  - domain: test
  - Components: COMP-005
  - Requirements: REQ-279
  - Acceptance: Tests assert argv is a list never a shell string, that an argument cannot inject a flag or a second command, that stdout JSON becomes ToolResult and stderr does not by itself mean failure, and that a non-zero exit maps to ToolOutcome.ERROR rather than raising. All fail.
- [ ] **T-888**: (green) CliAttachment over ToolTransport.PROCESS — the default attachment (D-569)
  - domain: backend
  - Components: COMP-005
  - Requirements: REQ-279
  - Acceptance: T-887 passes. Each declared CLI command is one named tool. argv is built from the manifest, never interpolated into a shell. Structured output is parsed; stderr is captured but is not itself a failure signal.
- [ ] **T-889**: (green) CLI failure handling: exit codes, unparseable output, missing binary
  - domain: backend
  - Components: COMP-005
  - Requirements: REQ-270, REQ-271
  - Acceptance: A non-zero exit becomes a ToolResult the agent can read and act on; a missing or unverified binary is a transport failure that raises; unparseable stdout never silently becomes an empty success.
- [ ] **T-890**: (green) Timeout, bounded retry with backoff, and circuit breaker on every external call
  - domain: backend
  - Components: COMP-005, COMP-006
  - Requirements: REQ-271, REQ-270
  - Acceptance: Both attachment implementations share one policy. A tripped breaker returns a structured error and does not deregister tools.
- [ ] **T-891**: (green) CliAttachment satisfies the hook Protocol, proven against a real binary
  - domain: backend
  - Components: COMP-005, COMP-004
  - Requirements: REQ-278, REQ-279
  - Acceptance: requirements/probe/describe_tools/invoke all satisfied with no core changes. Proven end to end against one real CLI, with its install directive, its declared tool surface, and its skill as three separate parts per D-570.
- [ ] **T-892**: (green) NativeAttachment loads an extension-provided entrypoint
  - domain: backend
  - Components: COMP-006, COMP-004
  - Requirements: REQ-278, REQ-279, REQ-264
  - Acceptance: A dotted entrypoint from the manifest is imported and adapted to the same Protocol. All service knowledge stays in the extension package.
- [ ] **T-893**: (red) Failing tests for the process launcher: env scrubbing, lazy start, idle reap, one path at every tier
  - domain: test
  - Components: COMP-008
  - Requirements: REQ-272, REQ-273, REQ-292
  - Acceptance: Tests assert LD_*, DYLD_*, PYTHONSTARTUP and NODE_OPTIONS are absent from the child environment and that no tier takes a different code path. All fail.
- [ ] **T-894**: (green) ProcessLauncher with SandboxPolicy and env safety filter
  - domain: infra
  - Components: COMP-008
  - Requirements: REQ-272, REQ-273, REQ-292
  - Acceptance: T-893 passes. Personal defaults to a no-op policy; stricter tiers swap the policy object only.
- [ ] **T-895**: (green) ArtifactPinVerifier checks version and hash before each execution
  - domain: backend
  - Components: COMP-009
  - Requirements: REQ-290
  - Acceptance: A mismatch refuses the run and emits an audit event naming expected and actual. A manifest without a pin cannot execute a third-party artifact.
- [ ] **T-896**: (red) Failing tests for CapabilityBridge: each tool registered by name, denied tools skipped without raising, classification defaults restrictive
  - domain: test
  - Components: COMP-012
  - Requirements: REQ-266, REQ-267, REQ-269
  - Acceptance: Tests drive the real ToolRegistry so a missing registration cannot pass. All fail.
- [ ] **T-897**: (green) CapabilityBridge converts tool specs into registered capabilities
  - domain: backend
  - Components: COMP-012
  - Requirements: REQ-266, REQ-267, REQ-269
  - Acceptance: T-896 passes. Each tool carries transport, classification, and capability tags, and rides the existing signed-call, policy, timeout and audit envelope unchanged.
- [ ] **T-898**: (green) Compose the manifest allowlist with the existing tool allow and deny filter
  - domain: backend
  - Components: COMP-012
  - Requirements: REQ-268, REQ-266
  - Acceptance: Deny wins across both layers. A denied tool is skipped with a policy-denied audit event rather than crashing startup.
- [ ] **T-899**: (red) Failing tests for ToolContractLedger: an altered description suspends the tool and demands re-approval
  - domain: test
  - Components: COMP-007
  - Requirements: REQ-291
  - Acceptance: Test approves a tool set, mutates a description on the controlled server, retrieves again, and asserts suspension plus audit. Fails.
- [ ] **T-900**: (green) ToolContractLedger hashing, comparison, and suspension
  - domain: backend
  - Components: COMP-007
  - Requirements: REQ-291
  - Acceptance: T-899 passes. Hash covers name, description and input schema. Upstream annotations are ignored in favour of the manifest.
- [ ] **T-901**: (green) Connector module scaffold: capabilities.py and _runtime.py, disabled by default
  - domain: backend
  - Components: COMP-015
  - Requirements: REQ-286
  - Acceptance: Module is discovered only when both files exist, activates only when configured, and requests the narrowest kwargs from signature-dispatched configure.
- [ ] **T-902**: (green) Module runtime state on a contextvar, never a module global
  - domain: backend
  - Components: COMP-015
  - Requirements: REQ-286, REQ-285
  - Acceptance: A second agent configured in a sibling asyncio task does not observe the first agent's state. Test forces interleaving rather than relying on ordering.

## Phase 3: Integration

- [ ] **T-903**: (red) Failing tests for the SecretStore seam: nothing written to config, log, prompt, or model context
  - domain: test
  - Components: COMP-010
  - Requirements: REQ-265, REQ-294
  - Acceptance: Tests inspect every written artifact for the secret value. All fail.
- [ ] **T-904**: (green) SecretStore interface with the local per-agent backend
  - domain: auth
  - Components: COMP-010
  - Requirements: REQ-265, REQ-294
  - Acceptance: T-903 passes. Owner-only permissions on file-backed storage; keyed by agent, instance and field.
- [ ] **T-905**: (green) Tier-selected secret backends behind the same interface
  - domain: auth
  - Components: COMP-010
  - Requirements: REQ-294
  - Acceptance: Selecting an external vault changes no calling code. Backend choice is configuration, not a branch at each call site.
- [ ] **T-906**: (red) Failing test for the credential renewal race under forced interleaving
  - domain: test
  - Components: COMP-011
  - Requirements: REQ-288
  - Acceptance: Two concurrent renewals against a rotating single-use token are held at a barrier; test asserts exactly one renewal in flight and that the persisted token is the accepted one. Fails.
- [ ] **T-907**: (green) CredentialLifecycle: proactive renewal, single-writer lock, atomic persist
  - domain: auth
  - Components: COMP-011
  - Requirements: REQ-287, REQ-288
  - Acceptance: T-906 passes. Renewal happens before expiry, never lazily on failure, and a crash mid-write cannot leave a torn credential.
- [ ] **T-908**: (green) Terminal versus transient renewal failure, and operator escalation
  - domain: auth
  - Components: COMP-011, COMP-013
  - Requirements: REQ-289
  - Acceptance: Transient errors retry with backoff; a failure needing re-consent stops retrying, marks the connection, and escalates through the operator approval path rather than agent chat.
- [ ] **T-909**: (green) ApprovalBinding: reads free and outbound gated by default, relaxable per instance
  - domain: auth
  - Components: COMP-013
  - Requirements: REQ-274, REQ-275
  - Acceptance: A gated call suspends, presents instance and outbound target, and resumes only on a signed operator grant pinned to the operator identity.
- [ ] **T-910**: (red) Failing tests for AuditRedactor: full capture, and a credential read that never records the value
  - domain: test
  - Components: COMP-014
  - Requirements: REQ-276, REQ-277
  - Acceptance: Tests assert inputs and outputs are present for an ordinary call and absent for a credential value. All fail.
- [ ] **T-911**: (green) AuditRedactor using the existing detector, applied before emission
  - domain: backend
  - Components: COMP-014
  - Requirements: REQ-276, REQ-277
  - Acceptance: T-910 passes. Redaction runs in the module, never inside the leaf audit package, so the dependency direction holds.
- [ ] **T-912**: (green) HostPrerequisiteDirector detects and instructs, never installs
  - domain: infra
  - Components: COMP-018
  - Requirements: REQ-262
  - Acceptance: A missing host prerequisite produces an explicit instruction naming what the operator must install. No implicit host mutation occurs in any path.
- [ ] **T-913**: (green) DependencyResolver: conflict refusal at install, reference-counted removal
  - domain: infra
  - Components: COMP-017
  - Requirements: REQ-264, REQ-284
  - Acceptance: An unsatisfiable version conflict between two extensions is refused at install rather than discovered at runtime. Removal drops only what no other extension declares.
- [ ] **T-914**: (green) Connector CLI: add, auth, list, tools, probe, doctor, approve, remove
  - domain: api
  - Components: COMP-016
  - Requirements: REQ-260, REQ-261, REQ-293
  - Acceptance: add prompts with non-echoing input, probes, and persists only on success. A failure at any step leaves no configuration, no secret, and no partial state, and names the failing step.

## Phase 4: Polish

- [ ] **T-915**: (red) Failing conformance test: a reference extension is added end to end with zero core files modified
  - domain: test
  - Components: COMP-021, COMP-004
  - Requirements: REQ-278, REQ-280
  - Acceptance: The governing test. A fixture extension implementing only the hook Protocol installs, registers tools, and executes a call. A git diff over core packages must be empty. Fails until the mechanism is genuinely general.
- [ ] **T-916**: (green) Architecture test: no vendor or service name appears in core packages
  - domain: test
  - Components: COMP-021
  - Requirements: REQ-280
  - Acceptance: Scan fails on any vendor identifier in core outside test fixtures, and names the offending file and line.
- [ ] **T-917**: (green) Removability tests: agent starts and passes smoke with all extensions removed, and again with all optional modules removed
  - domain: test
  - Components: COMP-021, COMP-015
  - Requirements: REQ-284, REQ-285, REQ-286
  - Acceptance: Both teardown paths leave a working agent with no residual configuration causing failure.
- [ ] **T-918**: (green) Management surfaces call the same functions as the CLI
  - domain: ui
  - Components: COMP-020
  - Requirements: REQ-293, REQ-295
  - Acceptance: Every web action has a command-line equivalent and routes through the same code path. Status is read from recorded state, never by probing. Secret entry posts directly to the store and never enters model context.
- [ ] **T-919**: (green) First protocol-server connection proven end to end against a real service
  - domain: mixed
  - Components: COMP-005, COMP-016, COMP-011
  - Requirements: REQ-260, REQ-266, REQ-287
  - Acceptance: Two accounts of one service run as two named instances; a real call succeeds; renewal survives expiry unattended. Evidence recorded in the bundle.
- [ ] **T-920**: (green) First direct-implementation connection proven end to end
  - domain: mixed
  - Components: COMP-006, COMP-016
  - Requirements: REQ-278, REQ-279, REQ-264
  - Acceptance: A service with no acceptable protocol upstream is reached through NativeAttachment, with all service knowledge inside the extension package.
- [ ] **T-921**: (red) Failing tests for the MCP client against a controlled server: stateless request shape, tools/list, tools/call
  - domain: test
  - Components: COMP-005
  - Requirements: REQ-279
  - Acceptance: Tests assert no initialize handshake, _meta protocol version present, and correct tools/list and tools/call envelopes. All fail.
- [ ] **T-922**: (green) McpAttachment — MCP client over httpx and stdio, 2026-07-28 stateless revision (D-569: the option, not the default)
  - domain: backend
  - Components: COMP-005, COMP-004
  - Requirements: REQ-278, REQ-279, REQ-270
  - Acceptance: T-921 passes. No vendor SDK. Satisfies the same hook Protocol as CliAttachment with no core changes — which is the proof the hook is transport-agnostic. A JSON-RPC error is a protocol failure; an isError result reaches the agent; an input_required result is never mistaken for completion.

## Traceability

| Requirement | Tasks |
|---|---|
| REQ-260 | T-884, T-885, T-914, T-919 |
| REQ-261 | T-884, T-914 |
| REQ-262 | T-877, T-878, T-912 |
| REQ-263 | T-884 |
| REQ-264 | T-877, T-878, T-884, T-892, T-913, T-920 |
| REQ-265 | T-903, T-904 |
| REQ-266 | T-896, T-897, T-898, T-919 |
| REQ-267 | T-896, T-897 |
| REQ-268 | T-877, T-878, T-880, T-881, T-898 |
| REQ-269 | T-877, T-878, T-896, T-897 |
| REQ-270 | T-889, T-890 |
| REQ-271 | T-889, T-890 |
| REQ-272 | T-893, T-894 |
| REQ-273 | T-893, T-894 |
| REQ-274 | T-877, T-878, T-909 |
| REQ-275 | T-909 |
| REQ-276 | T-910, T-911 |
| REQ-277 | T-910, T-911 |
| REQ-278 | T-879, T-891, T-892, T-915, T-920 |
| REQ-279 | T-879, T-887, T-888, T-891, T-892, T-920 |
| REQ-280 | T-879, T-915, T-916 |
| REQ-281 | T-882, T-883 |
| REQ-282 | T-882, T-883 |
| REQ-283 | T-882, T-883 |
| REQ-284 | T-913, T-917 |
| REQ-285 | T-902, T-917 |
| REQ-286 | T-901, T-902, T-917 |
| REQ-287 | T-907, T-919 |
| REQ-288 | T-906, T-907 |
| REQ-289 | T-908 |
| REQ-290 | T-877, T-878, T-895 |
| REQ-291 | T-886, T-899, T-900 |
| REQ-292 | T-893, T-894 |
| REQ-293 | T-914, T-918 |
| REQ-294 | T-903, T-904, T-905 |
| REQ-295 | T-886, T-918 |

## Open Questions

- Does the credential broker (the connector never receives the credential) land here or in a follow-on? COMP-010 is designed as the seam it would slot into, so the decision changes how aggressively T-904 and T-905 hand out handles rather than values.
- What is the retention and access policy for an audit store that now holds full message bodies? Full capture is required for reconstruction; protecting the store itself is not designed in this spec.
- Should a manifest tier floor be able to raise a deployment's effective stringency for that one connection, or only refuse to load below it?
- Phase 4 proves two connection shapes. The remaining services are deliberately not tasked here — they are additions through the mechanism, and if any of them requires a core change, T-915 has failed and the mechanism is wrong.
