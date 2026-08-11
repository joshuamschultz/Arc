# Product Requirements Document: Gateway Messaging + Media

## Context References

- **Personas:** [.claude/steering/product.md#user-personas](../../steering/product.md#user-personas)
- **Constraints:** [.claude/steering/product.md#business-constraints](../../steering/product.md#business-constraints)
- **Metrics Framework:** [.claude/steering/product.md#success-metrics-framework](../../steering/product.md#success-metrics-framework)
- **Current Phase:** [.claude/steering/roadmap.md#current-phase](../../steering/roadmap.md#current-phase)

## Product Overview

### Vision
Every surface an operator talks to Arc through — CLI, arcui, Telegram, Slack — is one messaging path that carries text and files in both directions, works on any launch path with no deployment step, and lets a platform be added or deleted as a folder.

### Problem Statement
Three things are broken and one is fragile. A photo sent to an agent produces no run at all: the Telegram adapter registers only a text handler, and there is no envelope for media to travel in even if it did. Every message starts a fresh run instead of joining the one already in flight, because the agent's delivery entry point is published only for teammates and no human surface calls it. arcui Messages silently shows nothing on any launch path except `arc … serve`, because that is the sole caller of the broker bootstrap. And each new platform is a separate package reimplementing the same security properties, so the fourth adapter is the fourth chance to forget a size cap.

### Value Proposition
Operators get the interaction they already expect: send a picture and the agent uses it, add a correction mid-task and it lands in the same conversation, open Messages and see the fleet. Maintainers get one place where download, naming, caps, audit, session keys and pairing are implemented, so a new platform cannot get them wrong.

## Personas

See `.claude/steering/product.md#user-personas`. Primary: the operator running a fleet from chat and browser. Secondary: the maintainer adding a platform. Constraints: `.claude/steering/product.md#business-constraints`. Current phase: `.claude/steering/roadmap.md#current-phase`.

## User Stories

- **US-1**: As an operator on Telegram, I want to send a photo or document and have the agent actually use it, so that I can hand it real material instead of describing it..
- **US-2**: As an operator mid-task, I want a follow-up message to join the work already running, so that I can correct or add to it without starting over..
- **US-3**: As an operator in arcui, I want Messages to work however I started Arc, so that the inbox is not silently empty depending on the launch command..
- **US-4**: As an operator, I want the agent to send files back, so that a produced report or chart reaches me on the channel I asked from..
- **US-5**: As a maintainer, I want to add or remove a platform as a folder, so that a new channel is a small adapter and never a second copy of the security rules..
- **US-6**: As a compliance reviewer, I want every artefact entering or leaving agent state to be attributable, so that the audit trail accounts for data movement, not just tool calls..
- **US-7**: As an agent on a team, I want a message I send a teammate to arrive as reliably as one a human sends me, so that delegation and hand-offs are not a second-class path that fails quietly..
- **US-8**: As an operator relying on Arc today, I want nothing I already use to stop working, so that a refactor of the messaging path is an improvement and never a trade..

## Functional Requirements

- **REQ-296** (story US-1, Must): WHEN a platform delivers a message containing any combination of text, images, and files THEN the gateway SHALL normalise it into one inbound envelope carrying an ordered list of typed parts, in which text is a part like any other.
- **REQ-297** (story US-1, Must): WHEN an inbound message carries media THEN the gateway SHALL write each artefact into the agent workspace using direct filesystem I/O and SHALL place a reference to it, never its bytes, into the envelope.
- **REQ-298** (story US-6, Must): The gateway SHALL compose every stored artefact's path itself from the date, time, resolved sender identity and a sanitised stem, and SHALL retain the sender-supplied filename as metadata only.
- **REQ-299** (story US-1, Must): IF an inbound artefact exceeds the configured size ceiling THEN the gateway SHALL refuse that artefact and SHALL tell the sender on the channel it arrived from.
- **REQ-300** (story US-6, Must): WHEN an artefact is stored from a channel or sent to one THEN the gateway SHALL emit one audit event naming the actor, channel, kind and size.
- **REQ-301** (story US-1, Must): The session history SHALL carry media references rather than media bytes, and the agent SHALL materialise bytes only for the provider call that needs them.
- **REQ-302** (story US-2, Must): WHEN a message arrives for a session whose interactive run is in flight THEN the agent SHALL deliver it into that run rather than beginning a separate one.
- **REQ-303** (story US-2, Must): IF no interactive run is in flight, or the only run in flight is a background run such as a schedule or a consolidation pass, THEN the message SHALL open a new turn in the same session and SHALL NOT interrupt that background work.
- **REQ-304** (story US-2, Must): The gateway SHALL own session identity for every surface, and a session SHALL rotate only when the operator sends an explicit new-session command.
- **REQ-305** (story US-6, Must): WHERE a channel is already paired or operator-authenticated the gateway SHALL treat it as the authorization boundary and SHALL NOT raise a human approval gate for each artefact carried on it.
- **REQ-306** (story US-3, Must): WHILE the gateway is starting it SHALL ensure a message broker is available before serving messaging, on every launch path and with no operator step beyond starting Arc.
- **REQ-307** (story US-3, Must): IF a broker cannot be started or reached THEN the messaging surfaces SHALL report an explicit unavailable state and SHALL NOT present an empty inbox as though it were an accurate one.
- **REQ-308** (story US-5, Must): Platform adapters SHALL live in the gateway tree as sibling folders and SHALL be discovered by scanning that directory for a declared platform descriptor.
- **REQ-309** (story US-5, Must): IF an adapter folder is absent, or present but fails to import THEN the gateway SHALL start normally, skip that platform, and log the roster it did load.
- **REQ-310** (story US-5, Must): An adapter SHALL implement only connection lifecycle, payload-to-parts translation, and parts delivery, and the gateway SHALL retain download, naming, size ceilings, audit, session identity, pairing and message splitting.
- **REQ-311** (story US-4, Must): WHEN an agent's reply contains media THEN the adapter SHALL deliver it through the same parts vocabulary as inbound, and IF the platform cannot carry that kind or size THEN the adapter SHALL degrade to a text description rather than losing the turn.
- **REQ-312** (story US-7, Must): An agent-to-agent message SHALL travel the same delivery path as a message from a human surface, so that injection, session identity and audit behave identically regardless of who sent it.
- **REQ-313** (story US-7, Must): WHEN the messaging layer accepts a message THEN it SHALL either deliver it to the addressee or record it in the dead-letter path with a reason, and SHALL NOT drop it silently.
- **REQ-314** (story US-7, Must): WHERE an addressee is offline when a message is sent THEN the message SHALL be retained and delivered when that agent next reads its mailbox, rather than being lost with the sender's turn.
- **REQ-315** (story US-8, Must): Every capability working before this feature SHALL work after it; changes SHALL be limited to addition, simplification, consolidation and re-routing, and SHALL NOT remove behaviour an operator depends on.
- **REQ-316** (story US-8, Must): The agent SHALL continue to write each turn to its session history in the workspace, and that history SHALL remain listable and replayable through the existing dashboard surfaces.
- **REQ-317** (story US-8, Must): WHEN re-routing leaves a module with no remaining caller THEN that module SHALL be deleted in the same change rather than left unreachable.

## MoSCoW Priorities

| Priority | Requirements |
|---|---|
| Must | REQ-296, REQ-297, REQ-298, REQ-299, REQ-300, REQ-301, REQ-302, REQ-303, REQ-304, REQ-305, REQ-306, REQ-307, REQ-308, REQ-309, REQ-310, REQ-311, REQ-312, REQ-313, REQ-314, REQ-315, REQ-316, REQ-317 |
| Should | _(none)_ |
| Could | _(none)_ |
| Won't | _(none)_ |

## Success Metrics

Framework: `.claude/steering/product.md#success-metrics-framework`. Targets for this feature: a photo sent to any paired channel produces a run that can read it, measured by the adapter contract suite passing for every discovered adapter; zero messages produce a second session while one is in flight; Messages returns a populated inbox on every launch path without an operator step; a platform folder can be deleted and the gateway starts clean; every stored or sent artefact has exactly one audit event. Agent-to-agent parity: a teammate message and a human message with the same content produce the same delivery outcome, injection decision and audit shape.

## Risks and Constraints

Reworking the inbound path touches the only thing currently making agents reachable, so a regression is a total outage of the surface rather than a degraded feature — mitigated by the adapter contract suite and by leaving session, runner and web untouched in this pass. Media in the workspace grows unbounded without retention, which is deliberately out of scope here and flagged. Folding three adapter packages in-tree changes install shape for anyone depending on them as distributions. Broker auto-start introduces a child process into the gateway lifecycle, which must be supervised and torn down or it outlives the parent. Agent-to-agent traffic already depends on the broker, so broker auto-start becomes load-bearing for team coordination and not only for the arcui inbox.

## Open Questions

- Resolved by REQ-317: the gateway's per-session inbound FIFO is deleted once delivery moves to the agent. It holds no session history — that is written separately by the agent to the workspace — so its removal cannot affect what the dashboard lists or replays. If flood backpressure is still wanted it belongs where turns are already serialised, in the agent's session coordinator, not in a second queue in the surface.
- Workspace media retention and pruning are unspecified: this feature writes artefacts but defines no lifetime for them.
- Uploading a repeatedly-referenced image once and passing a provider file identifier is a real cost saving but belongs to the model layer, not the gateway, and is deferred.
