# ADR-030: Agents get MCP capability; extensions plug in from outside

**Status**: Accepted
**Date**: 2026-08-04
**Supersedes**: the MCP-client exclusion in ADR-018 (migration tooling and the ACP adapter remain excluded)
**Decisions**: D-540 to D-566 in `.claude/decisions-log.md`

## Context

ADR-018 excluded an MCP client on three grounds: users adopt Arc fresh rather than bridging
from MCP-enabled tools, `AdapterBase` already accepts community adapters, and no federal or
enterprise pilot had asked for it. Each has since been overtaken.

Agents cannot reach the systems where work lives — tickets, docs, mail, calendar, files. That
blocks the product thesis ("anyone can turn arcagent into anything, quickly and
non-technically"), blocks the fleet from doing real work, and blocks the operator's own daily
use. Research across the ten target services found that the maintained integrations are MCP
servers, so "the community can write an adapter" resolves to "adopt MCP" in practice.

Three technical facts also changed:

- The MCP specification revision of 2026-07-28 removed sessions and the `initialize` handshake.
  The protocol is stateless. A correct client over httpx is roughly 150-400 LOC, plus 300-500
  if we implement OAuth 2.1 ourselves. The cost that justified deferral is much smaller.
- `ToolTransport` already declares `NATIVE | MCP | HTTP | PROCESS`
  (`arcagent/tools/_transport.py:27-33`) and `MCPServerEntry` already exists
  (`arcagent/core/config.py:152-158`). MCP is an unfinished transport, not a missing
  abstraction. Confirmed dead: the only construction site in package source is
  `transport=ToolTransport.NATIVE` at `agent_lifecycle.py:349`.
- MCP calls entering the registry ride the existing envelope unchanged — signed `ToolCall`,
  `caller_did`, `PolicyPipeline`, `HumanGate`, audit — so this adds reach without adding a
  second trust path.

## Decision

**Arc agents get MCP capability.** MCP dispatch is finished as a real transport, delivered as an
optional module so an agent without it still runs.

**Extensions live entirely outside Arc.** An extension is a self-contained, pluggable unit. Arc
provides hooks it can attach to and nothing else. A third party can build one without any
change to core, and removing every extension leaves a working agent.

**The mechanism is general; the connectors are examples.** We are building one way to add
external tools, interactions, and connections — not ten integrations. The ten named services
exist to prove the mechanism handles the real shapes (hosted and local, MCP and direct API,
OAuth and service account, read-mostly and write-heavy). They are test cases, not the design
target.

**The governing test:** the eleventh connector must be addable without touching Arc. If adding
one requires a change to core, the mechanism is wrong and the fix belongs in the hooks, never in
a special case for that service. Service-specific knowledge — which endpoint, which scope, which
auth dance — lives inside the extension where it belongs. Core never learns a vendor's name.

**Where a thing goes is decided by what it is:**

| What it is | Where it goes |
|---|---|
| Something the **machine** needs (a runtime, a binary, a system package) | The extension **directs the install** on the host running Arc |
| **Skills and tools** the agent uses | Loaded as agent tools and skills in the agent's **capability folder** |
| **Everything else** — implementation code, SDK clients, vendored dependencies | The **extension folder**, referenced from the agent's tools |

`arcagent` therefore contains only what every extension reuses: the hooks. Nothing
service-specific enters core. The one standing exception is the connector/adapter class itself,
as the Slack and Telegram gateway adapters already are.

## Rationale

- **Reach is the blocker, not protocol surface.** An agent that cannot touch a ticket or an
  inbox is advice, not work. Every other capability compounds only after this one exists.
- **Finishing beats adding.** The transport enum, the config model, and the dispatch envelope
  already exist. This closes a half-wired path rather than opening a new one.
- **The envelope is the product.** Per-tool-call authorization is Arc's differentiator. An MCP
  tool registered by name (`jira.create_issue`) is visible to policy, audit, and the approval
  gate; a generic passthrough tool would not be, which is why tools register individually.
- **Placement follows removability.** Machine-level installs are the host's business, agent
  capabilities belong in the capability folder the agent already loads, and implementation stays
  inside the extension so deleting the extension deletes it. Core stays flat and the LOC budget
  does not grow per connector.
- **Outside-in keeps the seam honest.** If an outside party can build an extension against the
  same hooks with no core change, the hooks are real. If they cannot, we have coupling we have
  not admitted.

## Consequences

**Positive**

- Agents reach Confluence, Jira, mail, calendar, and file storage.
- Adding a capability becomes configuration rather than engineering.
- Blueprints can declare the systems a persona works in, so a persona ships whole.
- Arc becomes an MCP host that verifies what it loads, authorizes per call, and signs what it
  does — which the MCP authorization spec, being pure OAuth 2.1 with no agent-instance identity,
  does not itself provide.
- Core LOC stays flat as connectors are added, because connectors add packages.

**Negative**

- We own an MCP client implementation and its conformance to a moving spec.
- Every adopted upstream is a standing maintenance commitment: pinned versions, advisories read,
  upgrades decided.
- Package pinning does not defend against tool poisoning (CVE-2025-54136), so a per-tool
  contract hash and re-approval flow must be built and maintained.
- Two of the ten services have no acceptable MCP upstream and need adapters written and
  maintained in-house.

**Neutral**

- Migration tooling and the ACP adapter stay excluded. ADR-018's reasoning on both is unchanged.

## Reconsider when

- **MCP client**: the spec churns badly enough that conformance cost exceeds the reach it buys,
  or the ecosystem consolidates on a successor protocol.
- **Extension placement**: a category of extension appears that fits none of the three
  placements — that is a signal the hooks are wrong, not that the rule needs a fourth row.
