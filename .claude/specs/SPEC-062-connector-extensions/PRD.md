# Product Requirements Document: SPEC-062 Connector Extensions — the general mechanism for external tools, interactions, and connections

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
One general way to attach any external system to an Arc agent. Arc exposes hooks; extensions live entirely outside Arc and plug into them. Adding the eleventh connection requires no change to core.

### Problem Statement
Arc agents can think, remember, message each other, and run workflows, but cannot touch a single system where the work actually lives — no ticket, no doc, no mail, no calendar, no file. MCP is scaffolded but dead: `ToolTransport` declares NATIVE|MCP|HTTP|PROCESS (arcagent/tools/_transport.py:27-33) and `MCPServerEntry` exists (core/config.py:152-158), yet the only construction site in package source is `transport=ToolTransport.NATIVE` (agent_lifecycle.py:349). The product thesis — anyone can turn arcagent into anything, quickly and non-technically — is unprovable while the agent connects to nothing.

### Value Proposition
Turns an agent from an advisor into a coworker. Makes adding a capability configuration rather than engineering, which is the extensibility thesis made demonstrable. Lets blueprints declare the systems a persona works in, so a persona ships whole. Produces an MCP host that verifies what it loads, authorizes per call, and signs what it does — which the MCP authorization spec, being pure OAuth 2.1 with no agent-instance identity, does not itself provide.

## Personas

See `.claude/steering/product.md#user-personas`. Primary: the operator running an agent fleet (Josh today; the five DGX agents). Secondary: a non-technical buyer evaluating whether an agent can do their job. Tertiary: a third-party developer building an extension against Arc's hooks without access to Arc's source. Constraint-setting: the federal operator who must harden later without a rewrite, per `.claude/steering/tech.md`.

## User Stories

- **US-1**: As an operator, I want to connect an agent to an external system with one command that asks me only for what it needs, so that the agent can act in that system without me writing code or editing config by hand..
- **US-2**: As an operator, I want every call an agent makes into an external system to pass the same identity, policy, and audit path as a native tool, so that reaching outside Arc never creates a second, weaker trust path..
- **US-3**: As an operator, I want consequential outbound actions to wait for my approval by default, so that a poisoned email or page cannot turn my agent into an exfiltration path..
- **US-4**: As a third-party developer, I want to build an extension against documented hooks without touching Arc's source, so that the mechanism is genuinely general rather than a set of hard-coded integrations..
- **US-5**: As an operator, I want to remove any module or any extension and still have a working agent, so that nothing I attach can become load-bearing for the agent itself..
- **US-6**: As an operator running agents unattended, I want connections to keep themselves alive and to page me only when a human is genuinely required, so that the fleet does not silently go dark when a token rotates..
- **US-7**: As a security-conscious operator, I want third-party connector code to be pinned, verified, contained, and never handed my credentials, so that adopting an upstream does not mean adopting its next compromise..

## Functional Requirements

- **REQ-260** (story US-1, Must): WHEN an operator runs the connector install command for an agent THEN the system SHALL read the extension manifest, prompt for each declared secret with non-echoing input, probe the connection, and persist configuration only after the probe succeeds.
- **REQ-261** (story US-1, Must): IF any step of install validation, probing, or signature verification fails THEN the system SHALL persist no configuration, no secret, and no partial state, and SHALL report which step failed.
- **REQ-262** (story US-1, Must): WHERE an extension declares a prerequisite the host machine must provide, the system SHALL direct the operator to install it on the host and SHALL NOT install it implicitly on the operator's behalf.
- **REQ-263** (story US-1, Must): WHERE an extension ships skills or tools for the agent, the system SHALL load them through the agent's capability folder using the existing capability-loading path.
- **REQ-264** (story US-1, Must): The system SHALL keep an extension's implementation code and its dependencies inside the extension's own folder, referenced from the agent's tools, and SHALL NOT copy them into core or into the agent's capability folder.
- **REQ-265** (story US-1, Must): The system SHALL write connector secrets only to the per-agent secret store, SHALL set file permissions to owner-only where the store is a file, and SHALL NOT write a secret into any configuration file, log, prompt, or model context.
- **REQ-266** (story US-2, Must): WHEN an extension's tools are loaded THEN the system SHALL register each allowlisted tool as an individually named capability so that policy, audit, and approval evaluate the specific verb rather than a generic dispatcher.
- **REQ-267** (story US-2, Must): WHEN a connector tool is dispatched THEN the system SHALL carry it through the existing envelope — argument validation, signed ToolCall bearing the caller DID, policy pipeline evaluation, timeout, and audit emission — with no path that bypasses any stage.
- **REQ-268** (story US-2, Must): IF an extension manifest requests an unbounded tool allowlist THEN the system SHALL refuse to load it above the personal tier and SHALL emit an audit event recording the refusal.
- **REQ-269** (story US-2, Must): The system SHALL require every registered connector tool to declare a read-only or state-modifying classification and the capability tags that determine its trifecta legs, defaulting to the more restrictive value when a declaration is absent.
- **REQ-270** (story US-2, Should): WHILE an extension's backing service or process is unavailable, the system SHALL keep that extension's tools registered and SHALL return a structured, agent-readable error rather than removing the tools from the agent's toolset.
- **REQ-271** (story US-2, Must): The system SHALL apply a timeout, bounded retry with exponential backoff, and a circuit breaker to every call into an external system.
- **REQ-272** (story US-2, Should): WHEN an agent first calls a tool belonging to an extension that runs a process THEN the system SHALL start that process on demand, and WHILE the process has been idle beyond its configured threshold the system SHALL stop it.
- **REQ-273** (story US-2, Must): WHEN the system spawns a process on an extension's behalf THEN it SHALL remove interpreter-startup and dynamic-loader environment variables from that process's environment before execution.
- **REQ-274** (story US-3, Must): The system SHALL provide a per-instance approval setting whose default admits inbound reads without approval and requires human approval for every outbound call, and SHALL allow an operator to relax it for a named connected account.
- **REQ-275** (story US-3, Must): WHEN a call requires approval under the effective approval setting THEN the system SHALL suspend execution, present the request naming the instance and the outbound target, and proceed only after a signed operator grant is recorded.
- **REQ-276** (story US-3, Must): The system SHALL record every connector call and its response, including inputs and outputs, in the audit trail with classification labels and encryption at rest.
- **REQ-277** (story US-3, Must): WHERE a connector call reads a credential, the system SHALL record the store, item, field, caller, and outcome, and SHALL NOT record the credential value.
- **REQ-278** (story US-4, Must): The system SHALL expose a documented hook contract sufficient for a third party to add a new external connection — its tools, its setup, and its credentials — without modifying any file inside arcagent or any other core package.
- **REQ-279** (story US-4, Must): The system SHALL require each extension manifest to declare how it attaches, and SHALL support at least a protocol-server attachment and a direct-implementation attachment through the same hooks.
- **REQ-280** (story US-4, Must): The system SHALL contain no vendor-specific or service-specific logic in core packages, and an automated architecture test SHALL fail if a vendor name appears in core outside of test fixtures.
- **REQ-281** (story US-4, Must): The system SHALL load extension bundles through an untrusted capability root so that the existing trust gate evaluates them, and SHALL NOT load them through a root that grants module-level trust.
- **REQ-282** (story US-4, Must): WHEN an extension bundle is loaded THEN the system SHALL verify its signature at load time independently of any install-time verification, and IF verification fails or raises above the personal tier THEN the system SHALL refuse the load and emit an audit event.
- **REQ-283** (story US-4, Must): IF a deployment requires extension signatures THEN the system SHALL also require a pinned verification key, and SHALL refuse to treat an unpinned signature requirement as satisfied.
- **REQ-284** (story US-5, Must): WHEN any or all extensions are removed THEN the agent SHALL start and operate normally without those extensions' features, with no residual configuration causing failure.
- **REQ-285** (story US-5, Must): WHEN any or all optional modules are removed THEN the agent SHALL start and operate normally without those modules' features.
- **REQ-286** (story US-5, Must): The system SHALL ship the connector-hosting module disabled by default so that an existing agent gains no new behavior until its configuration enables it.
- **REQ-287** (story US-6, Should): WHILE a connected account holds a credential with a known expiry, the system SHALL renew it before expiry rather than waiting for a call to fail.
- **REQ-288** (story US-6, Must): The system SHALL permit at most one credential renewal in flight per connected account and SHALL persist the renewed credential atomically, so that concurrent renewals cannot invalidate a working credential or leave a partially written one.
- **REQ-289** (story US-6, Should): IF a credential renewal fails in a way that requires human re-consent THEN the system SHALL stop retrying, mark the connection as needing attention, and raise it through the operator approval path rather than through agent chat.
- **REQ-290** (story US-7, Must): The system SHALL require an extension manifest to pin an exact version and integrity hash for any third-party artifact it runs, SHALL verify that hash before each execution, and IF the hash does not match THEN the system SHALL refuse to run it and emit an audit event.
- **REQ-291** (story US-7, Must): WHEN an extension's tool definitions are retrieved THEN the system SHALL compare a hash of each tool's name, description, and input schema against the hash recorded at approval, and IF a tool's hash differs THEN the system SHALL suspend that tool, emit an audit event, and require explicit re-approval before it can be called.
- **REQ-292** (story US-7, Must): The system SHALL run every extension-spawned process through a single launcher whose confinement is determined by a policy value, SHALL default that policy to no confinement at the personal tier, and SHALL NOT provide a separate execution path for any tier.
- **REQ-293** (story US-7, Must): The system SHALL make every connector management action — install, authorize, allowlist, inspect, approve, and remove — available from the command line and the terminal interface, and SHALL NOT require the web interface for any of them.
- **REQ-294** (story US-7, Should): The system SHALL resolve connector credentials through a single interface whose backing store is selected by deployment tier, defaulting to the local per-agent store and supporting an external vault without changes to calling code.
- **REQ-295** (story US-1, Should): The system SHALL record each connection's health, last successful use, and credential expiry in the operational data store so that management surfaces can report status without probing the external service.

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-260, REQ-261, REQ-262, REQ-263, REQ-264, REQ-265, REQ-266, REQ-267, REQ-268, REQ-269, REQ-271, REQ-273, REQ-274, REQ-275, REQ-276, REQ-277, REQ-278, REQ-279, REQ-280, REQ-281, REQ-282, REQ-283, REQ-284, REQ-285, REQ-286, REQ-288, REQ-290, REQ-291, REQ-292, REQ-293 |
| Should | REQ-270, REQ-272, REQ-287, REQ-289, REQ-294, REQ-295 |
| Could | _(none)_ |
| Won't | _(none)_ |

## Success Metrics

Framework in `.claude/steering/product.md#success-metrics`; quality gates in `.claude/steering/tech.md`. Targets for this feature: (1) GOVERNING METRIC — a new connection is added end to end with zero lines changed in any core package, demonstrated by an architecture test that fails if core references a vendor name; (2) an operator completes install-to-first-successful-call in under 5 minutes for a service whose credentials they already hold; (3) 100% of connector calls appear in the audit trail carrying caller DID, tool name, and outcome; (4) 0 credential values present in audit records, asserted by test; (5) agent cold start stays under 500ms with connectors configured but unused; (6) removing all extensions and all optional modules leaves a passing agent smoke test; (7) line coverage >= 80% and branch coverage >= 75% on new code, core components >= 90%.

## Risks and Constraints

Tool poisoning is the top risk: an upstream can serve a clean tool list at approval and change it later (CVE-2025-54136, >60% attack success across 45+ servers in CSA testing), and package pinning cannot defend hosted servers — mitigated by REQ-291. Supply chain generally: an OX Security scan found 43% of public MCP servers with command injection and 82% with path traversal, so every upstream is presumed hostile — mitigated by REQ-281, REQ-282, REQ-290, REQ-292. Credential exposure to third-party code — mitigated by REQ-265 and REQ-294, with a broker design under consideration in the SDD. Rotating single-use refresh tokens can be permanently destroyed by concurrent renewal — mitigated by REQ-288. Full audit capture (REQ-276) makes the audit store the highest-value target on the host and requires its own access control. Scope is large: an MCP client, a module, a loader, a manifest, CLI verbs, a secret store, a sandbox launcher, and contract hashing — mitigated by phasing the PLAN so the mechanism is provable before breadth is added. Constraints: core stays under 3,500 LOC (ADR-004); dependencies point one way and never up; `arctrust` is a leaf and cannot import `arcllm`, so payload redaction must happen before audit emission.

## Open Questions

- Does a self-contained extension vendor its own dependencies, or declare them for the host to install? Vendoring gives true removability and per-extension isolation; declaring is lighter but shares one dependency tree. This is the first question the SDD must answer.
- What is the precise shape of the hook contract — the Protocol or ABC that a protocol-server extension, a direct-implementation extension, and a skills-only extension all satisfy? REQ-278 states the requirement; the SDD owns the design.
- Where does the guided-setup TOML writer live, given `_dump_toml` is already duplicated between `arcgateway/connect.py:97-130` and `arccli/blueprints.py:461-483` — this feature is the third instance and the three-instances rule fires.
- Does the credential broker (connector never receives the credential) land in this spec or a follow-on? It is the strongest available answer to the supply-chain risk but adds a component.
