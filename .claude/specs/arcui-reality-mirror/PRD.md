# Product Requirements Document: ArcUI Reality Mirror

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
ArcUI is a faithful, manageable window onto the running Arc system: every list it renders (memories, channels, skills, tools) is the runtime's own answer, and every mutation it offers is the same authorized, audited operation the CLI performs. North star: ArcUI is full view/edit — every agent-defining artifact (identity files, workspace files, memories and their metadata, channels, capabilities) is viewable and, where safe and audited, editable in place.

### Problem Statement
Three ArcUI views have drifted from reality on live deployments: the Knowledge view reads a retired file format while memories live in the arcmemory database (index.db episodic store + entity graph), so operators see nothing; /api/team/channels fails open to an empty list when the embedded messaging-service handle is missing, hiding real channels that `arc team channels` lists; and the skills view globs a stale workspace/skills/*.md path instead of the four CapabilityLoader scan roots, so operator- and agent-added skills (e.g. a signed browserbase skill) are invisible and trust denials (tofu: deny) are silent. Each view reimplemented discovery instead of consuming the runtime's APIs, and each drifted independently.

### Value Proposition
Operators can trust the dashboard: what the fleet actually knows, which channels actually exist, and which capabilities are actually loaded (or refused, and why) are all visible and manageable in one place — no SSH required. Restores the product principle that ArcUI mirrors reality, and adds the first UI mutation surface (memory curation, channel management) with full authorize+audit parity with the CLI.

## Personas

See `.claude/steering/product.md#user-personas`. Primary: the deployment operator (personal-tier owner-operator like Josh on the DGX fleet; enterprise/federal ops staff). Secondary: security auditor reviewing what agents know and can do.

## User Stories

- **US-1**: As an operator, I want to browse an agent's memories, entities, and their links with full metadata (created date, decay/recency, scores, sources) and search across them, so that I can understand and audit what my agents actually know..
- **US-2**: As an operator, I want to edit or delete individual memory entries from the UI, so that I can correct or purge wrong, stale, or sensitive knowledge without SSH access..
- **US-3**: As an operator, I want to see the team's real channels and create channels or change their members from the UI, so that I can organize agent collaboration (work/personal/brand) without the CLI..
- **US-4**: As an operator, I want the skills and tools views to list everything the runtime discovered, from every scan root, with each item's source and trust/load status, so that I can see at a glance what my agents can do and why a capability was refused..
- **US-5**: As an operator, I want to view and edit my agents' identity and workspace files (identity.md and other agent-defining documents) directly in the UI, so that I can tune personas, boundaries, and agent configuration without SSH..

## Functional Requirements

- **REQ-084** (story US-1, Must): WHEN an operator opens an agent's Knowledge view THEN the system SHALL list that agent's episodic memories and entities read through arcmemory's query APIs, including created timestamp, recency/decay indicator, importance/bullet score (1-10), and source reference for each entry.
- **REQ-085** (story US-1, Must): WHEN an operator selects an entity or memory THEN the system SHALL display its linked entities and memories and SHALL allow navigation to each linked item.
- **REQ-086** (story US-1, Must): WHEN an operator submits a search query in the Knowledge view THEN the system SHALL return matching memories and entities for that agent ranked by the same relevance arcmemory's query API provides.
- **REQ-087** (story US-1, Must): The arcui package SHALL access memory data exclusively through arcmemory's public query/mutation APIs and SHALL NOT contain SQL against index.db.
- **REQ-088** (story US-2, Must): WHEN an operator-token-authenticated user edits or deletes a memory entry THEN the system SHALL apply the mutation via arcmemory's API and SHALL emit an audit event recording actor, agent, entry id, and operation; WHEN a viewer-token user attempts a mutation THEN the system SHALL refuse with 403.
- **REQ-089** (story US-2, Should): IF a memory mutation fails in arcmemory THEN the system SHALL surface the failure to the operator verbatim and SHALL NOT report partial success.
- **REQ-090** (story US-3, Must): WHEN the embedded server starts with a team root THEN the system SHALL wire the arcteam messaging service into the channels routes, and WHEN the service is unavailable THEN /api/team/channels SHALL return an explicit error state rather than an empty list.
- **REQ-091** (story US-3, Must): WHEN an operator-token-authenticated user creates a channel THEN the system SHALL create it via the same arcteam service call as `arc team create-channel`, SHALL refuse duplicate channel names, and SHALL emit an audit event.
- **REQ-092** (story US-3, Must): WHEN an operator-token-authenticated user adds or removes a channel member THEN the system SHALL apply the change via the arcteam service and SHALL emit an audit event naming actor, channel, and member DID.
- **REQ-093** (story US-4, Must): WHEN an operator opens an agent's skills or the fleet capability view THEN the system SHALL enumerate every skill discovered across all four CapabilityLoader scan roots (package builtins, global capabilities, agent capabilities, workspace capabilities) with name, version, description, and source root.
- **REQ-094** (story US-4, Must): The capability views SHALL display each item's trust/load status exactly as the runtime loader reports it (loaded, tofu-denied, invalid, or any future verdict) and SHALL NOT hardcode or infer status values in arcui.
- **REQ-095** (story US-4, Must): WHEN an operator opens an agent's tools view THEN the system SHALL list every tool registered for that agent at runtime, including capability-file tools and self-authored tools, not only built-in presets.
- **REQ-096** (story US-4, Must): The arcui package SHALL obtain capability inventories through a single seam over arcagent's discovery machinery and SHALL NOT glob skill or tool paths itself.
- **REQ-097** (story US-1, Should): WHILE an agent has no memory database yet the Knowledge view SHALL render an explicit empty state distinguishing 'no memories recorded' from 'memory store unreadable'.
- **REQ-098** (story US-4, Must): WHEN this feature ships THEN docs/deploy/single-node.md, docs/deploy/team-building.md, the README ArcUI section, and the arcui + root changelogs SHALL describe the new Knowledge, channel-management, and capability views.
- **REQ-099** (story US-5, Must): WHEN an operator-token-authenticated user edits an agent workspace file (e.g. identity.md) in the UI THEN the system SHALL save it subject to the same workspace-confinement and secret-content guards as agent tools and SHALL emit an audit event; WHEN a viewer-token user attempts the edit THEN the system SHALL refuse with 403.
- **REQ-100** (story US-2, Should): WHEN an operator adjusts a memory entry's metadata (importance score, decay-relevant fields) THEN the system SHALL apply the change via the arcmemory operator facade and SHALL emit an audit event.

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-084, REQ-085, REQ-086, REQ-087, REQ-088, REQ-090, REQ-091, REQ-092, REQ-093, REQ-094, REQ-095, REQ-096, REQ-098, REQ-099 |
| Should | REQ-089, REQ-097, REQ-100 |
| Could | _(none)_ |
| Won't | _(none)_ |

## Success Metrics

Mirror fidelity on the reference deployment (DGX 4-agent fleet): UI channel list matches `arc team channels` 1:1; UI skills list matches `arc agent skills` + loader log verdicts 1:1 including tofu-denied items; Knowledge view renders >0 entries for an agent with a populated index.db. Zero fail-open-empty responses: service-missing states render as errors, never []. 100% of UI mutations emit audit events (verified in tests). All suites green: arcui backend + tsc + eslint; mypy --strict; ruff.

## Risks and Constraints

arcmemory may lack a public query/mutation API surface for some needed reads (entity links, decay metadata) — mitigation: add the minimal read/mutation surface to arcmemory itself first (owned task in PLAN), keeping arcui consumer-only. Parallel TOFU-gate rework (task 16) changes status vocabulary — mitigation: REQ-094 renders whatever the loader reports, no enum hardcoding. arcui frontend bundle must be rebuilt deterministically — mitigation: build step documented and CI-checkable. Embedded channels wiring touches the same bootstrap seam recently modified by pairing work — mitigation: rebase-verify against branch head before implementation.

## Open Questions

_(none)_
